#!/usr/bin/env python3
"""Piloto de generación de manifiestos Kubernetes a partir de un modelo UVL.

Solo realiza dos tareas:
  1. Construir configuraciones parciales para los casos del perfil JSON.
  2. Utilizar main_k8s05.py para completar cada configuración y escribir un YAML.

No valida sintaxis YAML, esquemas Kubernetes ni utiliza kubectl.
El modo --plan permite revisar los JSON sin cargar el modelo de características.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CSV_COLUMNS = [
    "case_id", "kind", "api_version", "variation_point", "requested_value",
    "choice_feature", "value_source", "status", "partial_json",
    "full_config_json", "manifest_yaml", "error", "elapsed_seconds",
]


@dataclass
class Case:
    case_id: str
    kind: str
    api_version: str
    root: str
    path: str
    value: Any
    decisions: dict[str, Any]
    choice_feature: str = ""
    source: str = ""
    error: str = ""


def load_module(name: str, filepath: Path) -> Any:
    if not filepath.is_file():
        raise FileNotFoundError(f"No se encuentra el script: {filepath}")
    spec = importlib.util.spec_from_file_location(name, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"No se puede importar: {filepath}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_json(filepath: Path) -> dict[str, Any]:
    with filepath.open(encoding="utf-8") as file:
        result = json.load(file)
    if not isinstance(result, dict):
        raise ValueError(f"Se esperaba un objeto JSON en {filepath}")
    return result


def safe_name(text: Any) -> str:
    return (re.sub(r"[^a-zA-Z0-9._-]", "_", str(text)).strip("._-") or "base")[:75]


def entries_for_root(entries: list[dict[str, Any]], root: str) -> dict[str, list[dict[str, Any]]]:
    """Agrupar los metadatos de un recurso por su configPath."""
    index: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        feature, path = entry.get("feature"), entry.get("configPath")
        if (isinstance(feature, str) and isinstance(path, str)
                and (feature == root or feature.startswith(root + "_"))):
            index.setdefault(path, []).append(entry)
    return index


def field_entry(index: dict[str, list[dict[str, Any]]], path: str) -> dict[str, Any] | None:
    candidates = [e for e in index.get(path, [])
                  if not e.get("synthetic", False)
                  and e.get("serializationRole") in {"property", "structural"}]
    properties = [e for e in candidates if e.get("serializationRole") == "property"]
    if len(properties) == 1:
        return properties[0]
    return candidates[0] if len(candidates) == 1 else None


def scalar_type_matches(value: Any, schema_type: str | None) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return False


def encode_property(index: dict[str, list[dict[str, Any]]], path: str,
                    value: Any) -> tuple[dict[str, Any], str, str]:
    """Obtener decisiones UVL exactas a partir de configPath y metadatos.

    Este piloto solo construye variaciones escalares y alternativas simples.
    Las estructuras de tipo array/map se dejan para una fase posterior.
    """
    parent = field_entry(index, path)
    if parent is None:
        raise ValueError(f"No se encuentra una propiedad inequívoca: {path}")
    if parent.get("serializationKind") in {"array", "map", "object"}:
        raise ValueError(f"El piloto no incluye estructuras complejas: {path}")
    if value is None or isinstance(value, (dict, list)):
        raise ValueError(f"Se esperaba un valor escalar en {path}: {value!r}")

    feature = parent["feature"]
    if "fixedValue" in parent and (type(parent["fixedValue"]) is not type(value)
                                    or parent["fixedValue"] != value):
        raise ValueError(f"El valor de {path} debe ser {parent['fixedValue']!r}")

    options = [e for e in index.get(path, [])
               if e.get("serializationRole") == "value" and e.get("synthetic", False)]
    exact = [e for e in options if "configValue" in e
             and type(e["configValue"]) is type(value) and e["configValue"] == value]
    if len(exact) == 1:
        chosen = exact[0]
        return ({feature: True, chosen["feature"]: True},
                chosen["feature"], chosen.get("configValueSource", "model"))
    if len(exact) > 1:
        raise ValueError(f"Alternativa ambigua en {path}={value!r}")
    if any("configValue" in e for e in options):
        allowed = [e["configValue"] for e in options if "configValue" in e]
        raise ValueError(f"Valor no representado en {path}: {value!r}; posibles: {allowed}")

    # Helpers tipados para oneOf/IntOrString; este modo sencillo exige un único candidato.
    typed = [e for e in options if scalar_type_matches(value, e.get("schemaType"))]
    if len(typed) == 1:
        return ({feature: True, typed[0]["feature"]: value},
                typed[0]["feature"], "typed-helper")
    if options:
        raise ValueError(f"No se puede resolver la representación de {path}={value!r}")

    if not scalar_type_matches(value, parent.get("schemaType")):
        raise ValueError(f"Valor incompatible con schemaType={parent.get('schemaType')!r}: {path}")
    return {feature: value}, feature, "scalar"


def make_cases(profile: dict[str, Any], mapper: Any) -> list[Case]:
    resources = profile.get("resources")
    if not isinstance(resources, list) or not resources:
        raise ValueError("El perfil necesita una lista no vacía 'resources'")
    cases: list[Case] = []
    for resource in resources:
        kind, api_version = resource["kind"], resource["apiVersion"]
        root = mapper.gvk_to_root.get((api_version, kind))
        if root is None:
            raise ValueError(f"El mapeador no reconoce el recurso {api_version}/{kind}")
        index = entries_for_root(mapper.serialization_entries, root)

        base: dict[str, Any] = {}
        for path, value in (("apiVersion", api_version), ("kind", kind)):
            decisions, _, _ = encode_property(index, path, value)
            base.update(decisions)
        # No forzamos ninguna raíz UVL: main_k8s añade sus ancestros automáticamente.
        if field_entry(index, "metadata.name") is not None:
            try:
                name, _, _ = encode_property(index, "metadata.name", f"fm-pilot-{kind.lower()}")
                base.update(name)
            except ValueError:
                pass

        points = resource.get("points", [])
        if not isinstance(points, list):
            raise ValueError(f"'points' no es una lista para {kind}")
        experiments = [("(base)", None)]
        for point in points:
            if not isinstance(point.get("values"), list):
                raise ValueError(f"El punto {point.get('path')!r} necesita 'values' como lista")
            experiments.extend((point["path"], value) for value in point["values"])

        for path, value in experiments:
            decisions = dict(base)
            feature, source, error = "", "", ""
            if path != "(base)":
                try:
                    choice, feature, source = encode_property(index, path, value)
                    decisions.update(choice)
                except ValueError as exc:
                    error = str(exc)
            cases.append(Case(
                case_id=f"{len(cases)+1:03d}_{safe_name(kind)}_{safe_name(path)}_{safe_name(value)}",
                kind=kind, api_version=api_version, root=root, path=path,
                value=value, decisions=decisions, choice_feature=feature,
                source=source, error=error,
            ))
    return cases


def save_csv(filepath: Path, rows: list[dict[str, Any]]) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with filepath.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def is_in_resource(feature: Any, resource_name: str) -> bool:
    current = feature
    while current is not None:
        if current.name == resource_name:
            return True
        current = current.get_parent()
    return False


def generate(cases: list[Case], rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    generator = load_module("pilot_manifest_generator", args.generator)
    required = (
        "load_configurations_with_collections", "UVLReader", "FmToPysat",
        "complete_and_minimize", "render_manifest", "write_manifest",
    )
    missing_api = [name for name in required if not hasattr(generator, name)]
    if missing_api:
        raise ValueError(
            f"El generador {args.generator} no implementa las funciones esperadas: {missing_api}"
        )
    print(f"Cargando UVL: {args.uvl}", flush=True)
    fm = generator.UVLReader(str(args.uvl)).transform()
    features = {feature.name: feature for feature in fm.get_features()}
    print("Traduciendo el modelo a SAT una sola vez...", flush=True)
    sat_model = generator.FmToPysat(fm).transform()

    for case, row in zip(cases, rows):
        if case.error:
            continue
        started = time.perf_counter()
        yaml_path = args.output_dir / "manifests" / f"{case.case_id}.yaml"
        full_path = args.output_dir / "full_configs" / f"{case.case_id}.json"
        # Evitar confundir archivos de ejecuciones anteriores con resultados nuevos.
        for old in (yaml_path, full_path):
            old.unlink(missing_ok=True)
        try:
            missing = [name for name in case.decisions if name not in features]
            if missing:
                raise ValueError(f"Features ausentes del UVL: {missing}")
            outside = [name for name in case.decisions
                       if not is_in_resource(features[name], case.root)]
            if outside:
                raise ValueError(f"Features fuera del recurso {case.root}: {outside}")

            # Reutilizamos el lector de main_k8s05: conserva arrays/mapas repetidos
            # fuera del SAT. El perfil inicial solo define valores escalares.
            partial_path = args.output_dir / "partial_configs" / f"{case.case_id}.json"
            partial, collections = generator.load_configurations_with_collections(
                fm, [str(partial_path)]
            )
            # La expansión total se desactiva explícitamente para los experimentos.
            completed = generator.complete_and_minimize(
                fm, sat_model, partial, expand_to_leaves=False,
            )
            comments = None
            if args.variability_comments:
                comments = generator.build_variability_comments(
                    fm, completed, set(generator.selected_items(partial))
                )
            manifest = generator.render_manifest(
                fm, completed, comments, collections=collections,
                expand_to_leaves=False, collection_mode="empty",
            )
            if not manifest.strip():
                raise ValueError("El generador devolvió un manifiesto vacío")

            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(json.dumps(completed.elements, ensure_ascii=False,
                                            indent=2) + "\n", encoding="utf-8")
            generator.write_manifest(manifest, yaml_path)
            row.update(status="generated", full_config_json=str(full_path),
                       manifest_yaml=str(yaml_path))
        except Exception as exc:
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            row["elapsed_seconds"] = f"{time.perf_counter() - started:.3f}"
            print(f"[{case.case_id}] {row['status']}", flush=True)
            save_csv(args.output_dir / "generation_results.csv", rows)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", type=Path, default=Path(__file__).with_name("pilot_profile.json"))
    p.add_argument("--descriptions", type=Path, required=True)
    p.add_argument("--metadata", type=Path, required=True)
    p.add_argument("--mapper", type=Path, default=Path("metadata_parser01.py"))
    p.add_argument("--generator", type=Path, default=Path("main_k8s05.py"))
    p.add_argument(
        "--variability-comments", action="store_true",
        help="Añadir los comentarios 'doc' y las alternativas seleccionadas.",
    )
    p.add_argument("--uvl", type=Path, help="Modelo UVL (obligatorio para generar YAML)")
    p.add_argument("--output-dir", type=Path, default=Path("experiments/pilot"))
    p.add_argument("--plan", action="store_true", help="Solo generar JSON parciales; no crear YAML")
    p.add_argument("--limit", type=int, help="Probar únicamente los N primeros casos")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.plan and not args.uvl:
        raise ValueError("Indica --uvl para generar los manifiestos o usa --plan")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit debe ser un entero positivo")

    mapper_module = load_module("pilot_metadata_mapper", args.mapper)
    mapper = mapper_module.MetadataMapper(str(args.descriptions), str(args.metadata))
    cases = make_cases(load_json(args.profile), mapper)
    if args.limit:
        cases = cases[:args.limit]

    rows = []
    output_partials = args.output_dir / "partial_configs"
    output_partials.mkdir(parents=True, exist_ok=True)
    for case in cases:
        row = {name: "" for name in CSV_COLUMNS}
        row.update(case_id=case.case_id, kind=case.kind, api_version=case.api_version,
                   variation_point=case.path,
                   requested_value=json.dumps(case.value, ensure_ascii=False),
                   choice_feature=case.choice_feature, value_source=case.source,
                   status="skipped" if case.error else "planned", error=case.error)
        if not case.error:
            filepath = output_partials / f"{case.case_id}.json"
            filepath.write_text(json.dumps(case.decisions, indent=2, ensure_ascii=False)
                                + "\n", encoding="utf-8")
            row["partial_json"] = str(filepath)
        rows.append(row)

    csv_path = args.output_dir / "generation_results.csv"
    save_csv(csv_path, rows)
    if not args.plan:
        generate(cases, rows, args)

    summary = {status: sum(r["status"] == status for r in rows)
               for status in ("generated", "failed", "skipped", "planned")
               if any(r["status"] == status for r in rows)}
    print(f"Casos: {len(rows)} | {summary}")
    print(f"Resultados: {csv_path}")
    if args.plan:
        print("Modo --plan: se han creado JSON parciales; no se ha cargado el UVL.")
    return 1 if any(r["status"] == "failed" for r in rows) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, ImportError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
