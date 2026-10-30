#!/usr/bin/env python3
"""Piloto reproducible de variación individual para Kubernetes + UVL.

Usa MetadataMapper (metadata_parser01.py) para resolver GVK -> feature raíz,
serialization_metadata para los nombres exactos de las features y
main_k8s_corregido.py para completar configuraciones y renderizar YAML.

Modos:
    plan (por defecto): solo metadatos; NO requiere FlamaPy/SAT.
    execute: carga UVL/SAT una vez y genera los manifiestos.

Las pruebas SAT, sintaxis YAML y aceptación por Kubernetes son distintas.
No presupone que el generador serialice correctamente arrays/mapas.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HEADERS = [
    "case_id", "api_version", "kind", "point_path", "requested_value",
    "case_type", "choice_feature", "source", "status", "sat_status",
    "yaml_syntax", "gvk_match", "kubectl_validation", "mapper_unmapped",
    "partial_json", "full_config_json", "yaml_file", "error", "elapsed_seconds",
]


@dataclass
class Case:
    case_id: str
    api_version: str
    kind: str
    root: str
    path: str
    value: Any
    decisions: dict[str, Any]
    choice_feature: str
    source: str
    case_type: str
    error: str = ""


def load_local_module(module_name: str, script: Path) -> Any:
    if not script.is_file():
        raise FileNotFoundError(f"No existe el script: {script}")
    spec = importlib.util.spec_from_file_location(module_name, str(script))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se puede importar el script: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def safe_name(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._-")
    return (result or "case")[:90]


def same_root(feature: str, root: str) -> bool:
    return feature == root or feature.startswith(root + "_")


def entries_for_root(entries: list[dict[str, Any]], root: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        feature, path = entry.get("feature"), entry.get("configPath")
        if isinstance(feature, str) and isinstance(path, str) and same_root(feature, root):
            result.setdefault(path, []).append(entry)
    return result


def field_entry(index: dict[str, list[dict[str, Any]]], path: str) -> dict[str, Any] | None:
    candidates = [
        e for e in index.get(path, [])
        if not e.get("synthetic", False)
        and e.get("serializationRole") in ("property", "structural")
    ]
    properties = [e for e in candidates if e.get("serializationRole") == "property"]
    return properties[0] if len(properties) == 1 else (
        candidates[0] if len(candidates) == 1 else None
    )


def matches(expected: Any, actual: Any) -> bool:
    # Coincidencia estricta: no confundir True con 1 ni '0' con 0.
    return type(expected) is type(actual) and expected == actual


def scalar_matches_type(value: Any, schema_type: str | None) -> bool:
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return False


def encode_property(index: dict[str, list[dict[str, Any]]], path: str,
                    value: Any) -> tuple[dict[str, Any], str, str]:
    """Construir decisiones UVL usando configPath y serializationRole.

    Rechaza arrays/mapas y valores ajenos a las alternativas modeladas.
    """
    parent = field_entry(index, path)
    if parent is None:
        raise ValueError(f"No hay una propiedad inequívoca para configPath={path!r}")
    feature = parent["feature"]
    if parent.get("serializationKind") in {"array", "map", "object"}:
        raise ValueError(f"Propiedad estructural no admitida en piloto: {path}")
    if isinstance(value, (dict, list)) or value is None:
        raise ValueError(f"Valor no escalar no admitido para {path}: {value!r}")
    if "fixedValue" in parent and not matches(parent["fixedValue"], value):
        raise ValueError(f"{path} tiene fixedValue={parent['fixedValue']!r}, no {value!r}")

    variants = [e for e in index.get(path, [])
                if e.get("serializationRole") == "value" and e.get("synthetic", False)]
    exact = [e for e in variants
             if "configValue" in e and matches(e["configValue"], value)]
    if len(exact) == 1:
        return ({feature: True, exact[0]["feature"]: True},
                exact[0]["feature"], exact[0].get("configValueSource", "model"))
    if len(exact) > 1:
        raise ValueError(f"Valor ambiguo: {path}={value!r}")
    # Algunos valores extraídos de descripciones aparecen serializados como texto.
    text_matches = [e for e in variants
                    if isinstance(e.get("configValue"), str)
                    and isinstance(value, str) and e["configValue"] == value]
    if len(text_matches) == 1:
        return ({feature: True, text_matches[0]["feature"]: True},
                text_matches[0]["feature"], text_matches[0].get("configValueSource", "model"))

    # Si hay alternativas explícitas no debemos inventar una alternativa distinta.
    explicit = [e for e in variants if "configValue" in e]
    if explicit:
        allowed = [e["configValue"] for e in explicit]
        raise ValueError(f"Variante no modelada para {path}: {value!r}; modeladas: {allowed!r}")

    # Unión tipada (IntOrString): elegir el helper con tipo compatible.
    typed = [e for e in variants if "configValue" not in e
             and scalar_matches_type(value, e.get("schemaType"))]
    if len(typed) == 1:
        return ({feature: True, typed[0]["feature"]: value},
                typed[0]["feature"], "typed-helper")
    if len(typed) > 1:
        raise ValueError(f"Varios helpers tipados para {path}")
    if variants:
        raise ValueError(f"No se puede representar {path}={value!r} con los helpers UVL")

    # Propiedad escalar (incluye parámetros numéricos obligatorios).
    schema_type = parent.get("schemaType")
    if not scalar_matches_type(value, schema_type):
        raise ValueError(f"Tipo de {path} incompatible: schemaType={schema_type!r}, valor={value!r}")
    return {feature: value}, feature, "scalar"


def make_cases(profile: dict[str, Any], mapper: Any) -> list[Case]:
    if not isinstance(profile.get("resources"), list) or not profile["resources"]:
        raise ValueError("El perfil debe incluir una lista no vacía 'resources'")
    cases: list[Case] = []
    for resource in profile["resources"]:
        kind, api_version = resource["kind"], resource["apiVersion"]
        key = (api_version, kind)
        root = mapper.gvk_to_root.get(key)
        if not root:
            raise ValueError(f"GVK no reconocido por MetadataMapper: {key!r}")
        index = entries_for_root(mapper.serialization_entries, root)
        # No seleccionar explícitamente ni Kubernetes ni la raíz Deployment/Service:
        # completar ancestros la incluirá, pero sin ampliar su subárbol opcional.
        base: dict[str, Any] = {}
        for path, value in (("apiVersion", api_version), ("kind", kind)):
            choice, _, _ = encode_property(index, path, value)
            base.update(choice)
        # metadata.name facilita construir manifiestos identificables; no presupone
        # que cualquier estructura obligatoria esté ya completa.
        if field_entry(index, "metadata.name"):
            try:
                selected, _, _ = encode_property(index, "metadata.name",
                                                  f"fm-pilot-{kind.lower()}")
                base.update(selected)
            except ValueError:
                pass  # Algunas raíces no tienen name configurable con esa ruta.

        spec = resource.get("points", [])
        if not isinstance(spec, list):
            raise ValueError(f"'points' debe ser una lista para {kind}")
        specs = [("(base)", None)] + [
            (point["path"], value)
            for point in spec for value in point["values"]
        ]
        for path, value in specs:
            decisions = dict(base)
            chosen, source, error = "", "", ""
            if path != "(base)":
                try:
                    selection, chosen, source = encode_property(index, path, value)
                    # Si el usuario pidió un valor distinto al de una semilla,
                    # el valor del caso tiene preferencia sobre esa semilla.
                    decisions.update(selection)
                except ValueError as exc:
                    error = str(exc)
            cases.append(Case(
                case_id=f"{len(cases)+1:03d}_{safe_name(kind)}_{safe_name(path)}_{safe_name(str(value))}",
                api_version=api_version, kind=kind, root=root,
                path=path, value=value, decisions=decisions,
                choice_feature=chosen, source=source,
                case_type="base" if path == "(base)" else "single_variation",
                error=error,
            ))
    return cases


def as_row(case: Case) -> dict[str, Any]:
    return {
        "case_id": case.case_id, "api_version": case.api_version,
        "kind": case.kind, "point_path": case.path,
        "requested_value": json.dumps(case.value, ensure_ascii=False),
        "case_type": case.case_type, "choice_feature": case.choice_feature,
        "source": case.source, "status": "skipped" if case.error else "planned",
        "sat_status": "not_run", "yaml_syntax": "not_run",
        "gvk_match": "not_run", "kubectl_validation": "not_run",
        "mapper_unmapped": "not_run", "partial_json": "",
        "full_config_json": "", "yaml_file": "",
        "error": case.error, "elapsed_seconds": "",
    }


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def validate_yaml(yaml_text: str, api_version: str, kind: str) -> tuple[str, str, str]:
    import yaml
    try:
        docs = list(yaml.safe_load_all(yaml_text))
    except yaml.YAMLError as exc:
        return "false", "not_run", str(exc)
    if len(docs) != 1 or not isinstance(docs[0], dict):
        return "false", "not_run", "Se esperaba un único documento YAML de tipo mapping"
    doc = docs[0]
    if doc.get("apiVersion") != api_version or doc.get("kind") != kind:
        return ("true", "false", "GVK generado distinto al solicitado: "
                f"{doc.get('apiVersion')!r}/{doc.get('kind')!r}")
    return "true", "true", ""


def run_kubectl(yaml_file: Path, timeout: int) -> tuple[str, str]:
    # No se aplica ningún recurso: --dry-run=server solo pide validación al API server.
    try:
        process = subprocess.run(
            ["kubectl", "apply", "--dry-run=server", "--validate=strict", "-f", str(yaml_file)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return "unavailable", str(exc)
    detail = (process.stderr or process.stdout).strip()[:900]
    return ("passed" if process.returncode == 0 else "failed"), detail


def execute_cases(cases: list[Case], rows: list[dict[str, Any]], args: Any,
                  mapper: Any) -> None:
    if not args.uvl:
        raise ValueError("Para --execute es obligatorio indicar --uvl")
    generator = load_local_module("pilot_k8s_generator", args.generator)
    print(f"Leyendo modelo UVL una sola vez: {args.uvl}", flush=True)
    fm = generator.UVLReader(str(args.uvl)).transform()
    features = {feature.name: feature for feature in fm.get_features()}
    print("Traduciendo UVL a SAT una sola vez...", flush=True)
    sat_model = generator.FmToPysat(fm).transform()

    out_partial = args.output_dir / "partial_configs"
    out_yaml = args.output_dir / "manifests"
    out_full = args.output_dir / "full_configs"
    out_full.mkdir(parents=True, exist_ok=True)
    out_partial.mkdir(parents=True, exist_ok=True)
    out_yaml.mkdir(parents=True, exist_ok=True)
    for case, row in zip(cases, rows):
        if case.error:
            continue
        start = time.perf_counter()
        missing = [name for name in case.decisions if name not in features]
        if missing:
            row.update(status="skipped", error=f"Features ausentes del UVL: {missing!r}")
            continue
        # Verificar la pertenencia REAL al subárbol (no solo el prefijo textual).
        def belongs(name: str) -> bool:
            current = features[name]
            while current is not None:
                if current.name == case.root:
                    return True
                current = current.get_parent()
            return False
        wrong = [name for name in case.decisions if not belongs(name)]
        if wrong:
            row.update(status="skipped", error=f"Fuera del recurso {case.root}: {wrong!r}")
            continue
        json_file = out_partial / f"{case.case_id}.json"
        yaml_file = out_yaml / f"{case.case_id}.yaml"
        if yaml_file.exists():
            yaml_file.unlink()  # No confundir ejecuciones nuevas y artefactos antiguos.
        json_file.write_text(json.dumps(case.decisions, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        row["partial_json"] = str(json_file)
        try:
            with generator.SatChecker(sat_model) as checker:
                if not checker.is_satisfiable(case.decisions):
                    row.update(status="unsatisfiable", sat_status="false",
                               error="La configuración parcial no satisface el FM")
                    continue
            row["sat_status"] = "true"  # SAT booleano; no valida valores numéricos.
            config = generator.Configuration(dict(case.decisions))
            full = generator.complete_and_minimize(fm, sat_model, config, expand_to_leaves=False)
            full_file = out_full / f"{case.case_id}.json"
            full_file.write_text(json.dumps(full.elements, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
            row["full_config_json"] = str(full_file)
            content = generator.render_manifest(fm, full)
            generator.write_manifest(content, yaml_file)
            row["yaml_file"] = str(yaml_file)
            syntax, match, err = validate_yaml(content, case.api_version, case.kind)
            row.update(yaml_syntax=syntax, gvk_match=match, status="generated")
            if err:
                row["error"] = err
            if args.roundtrip and syntax == "true" and match == "true":
                try:
                    import yaml
                    mapper.transform_manifest(yaml.safe_load(content))
                    report = mapper.mapping_report()
                    row["mapper_unmapped"] = str(len(report["unmapped"]))
                except Exception as exc:
                    row["mapper_unmapped"] = "failed"
                    row["error"] = (row["error"] + " | " if row["error"] else "") + \
                                   f"Mapper: {type(exc).__name__}: {exc}"
            if args.kubectl_server and syntax == "true" and match == "true":
                kubectl_status, detail = run_kubectl(yaml_file, args.timeout)
                row["kubectl_validation"] = kubectl_status
                if kubectl_status != "passed":
                    row["error"] = (row["error"] + " | " if row["error"] else "") + detail
        except Exception as exc:
            row["status"] = "generation_failed"
            row["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            row["elapsed_seconds"] = round(time.perf_counter() - start, 3)
            # Guardar resultados incluso si una ejecución se interrumpe a mitad.
            write_rows(args.output_dir / "experiment_results.csv", rows)


def parse_args() -> Any:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(__file__).with_name("pilot_profile.json"))
    parser.add_argument("--descriptions", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--mapper", type=Path, default=Path(__file__).with_name("metadata_parser01.py"))
    parser.add_argument("--generator", type=Path, default=Path(__file__).with_name("main_k8s_corregido.py"))
    parser.add_argument("--uvl", type=Path, help="Necesario solo con --execute")
    parser.add_argument("--output-dir", type=Path, default=Path("experiments/pilot"))
    parser.add_argument("--execute", action="store_true", help="Generar YAML (por defecto solo plan)")
    parser.add_argument("--roundtrip", action="store_true", help="Diagnóstico YAML -> JSON, no equivalencia")
    parser.add_argument("--kubectl-server", action="store_true",
                        help="Validación contra un clúster mediante kubectl --dry-run=server")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s: %(message)s")
    if args.timeout < 1:
        raise ValueError("--timeout debe ser positivo")
    mapper_module = load_local_module("pilot_metadata_mapper", args.mapper)
    mapper = mapper_module.MetadataMapper(str(args.descriptions), str(args.metadata))
    profile = load_json(args.profile)
    cases = make_cases(profile, mapper)
    rows = [as_row(case) for case in cases]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_rows(args.output_dir / "experiment_results.csv", rows)
    if args.execute:
        execute_cases(cases, rows, args, mapper)
    else:
        # Para revisar las decisiones propuestas antes de lanzar el SAT costoso.
        folder = args.output_dir / "partial_configs"
        folder.mkdir(parents=True, exist_ok=True)
        for case, row in zip(cases, rows):
            if case.error:
                continue
            file = folder / f"{case.case_id}.json"
            file.write_text(json.dumps(case.decisions, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
            row["partial_json"] = str(file)
        write_rows(args.output_dir / "experiment_results.csv", rows)
    status_count: dict[str, int] = {}
    for row in rows:
        status_count[row["status"]] = status_count.get(row["status"], 0) + 1
    print(f"Casos: {len(cases)} | Estado: {status_count}")
    print(f"Resultados: {args.output_dir / 'experiment_results.csv'}")
    if not args.execute:
        print("Solo plan: no se ha cargado el UVL ni se ha ejecutado SAT o Kubernetes.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)