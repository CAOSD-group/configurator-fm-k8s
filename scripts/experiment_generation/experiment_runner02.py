#!/usr/bin/env python3
"""Preparar JSON parciales y opcionalmente YAML de perfiles UVL generados con FMVariationPoints.

Compatibilidad con experiment_runner.py: lee points[{path,values}] y añade
feature_cases[{path,variant_feature,decisions}]. --plan NO ejecuta SAT ni genera YAML.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import experiment_runner as base


def cases_from_profile(profile: dict[str, Any], mapper: Any) -> list[base.Case]:
    # Primero creamos la base y todas las variaciones por valor (v2).
    # Las variaciones booleanas explícitas se añaden después, usando la misma base.
    cases = base.make_cases(profile, mapper)
    if len(profile['resources']) != 1:
        raise ValueError('Este piloto acepta exactamente un recurso por perfil')
    resource = profile['resources'][0]
    kind, version = resource['kind'], resource['apiVersion']
    root = mapper.gvk_to_root[(version, kind)]
    initial = next((c for c in cases if c.path == '(base)'), None)
    if initial is None:
        raise ValueError('El perfil no contiene un caso base')
    for idx, option in enumerate(resource.get('feature_cases', []), start=1):
        if not isinstance(option, dict):
            raise ValueError('feature_cases debe ser una lista de objetos')
        decisions = option.get('decisions')
        if not isinstance(decisions, dict) or not decisions:
            raise ValueError(f'feature_cases[{idx}].decisions debe ser un objeto no vacío')
        if any(not isinstance(key, str) or not isinstance(value, (str, bool, int, float))
               for key, value in decisions.items()):
            raise ValueError('feature_cases requiere nombres UVL y valores escalares concretos')
        # La selección estructural no se transforma a paths mediante encode_property;
        # utiliza los nombres exactos de las features del modelo.
        combined = dict(initial.decisions)
        conflict = [key for key, value in decisions.items()
                    if key in combined and combined[key] != value]
        if conflict:
            raise ValueError(f'Un caso modifica una decisión obligatoria/base: {conflict}')
        combined.update(decisions)
        feature = str(option.get('variant_feature') or next(iter(decisions)))
        value = option.get('value', True)
        path = str(option.get('path', '') or option.get('point_feature') or feature)
        cases.append(base.Case(
            case_id=f'{len(cases)+1:04d}_{base.safe_name(kind)}_'
                    f'{base.safe_name(path)}_{base.safe_name(feature)}_{base.safe_name(value)}',
            kind=kind, api_version=version, root=root, path=path, value=value,
            decisions=combined, choice_feature=feature,
            source='FMVariationPoints:' + str(option.get('relation_type', 'structural')),
        ))
    # Los arrays usan el formato estructurado que entiende main_k8s05, y además
    # imponen la variante al SAT mediante los nombres de features a nivel raíz.
    for idx, option in enumerate(resource.get('array_cases', []), start=1):
        if not isinstance(option, dict):
            raise ValueError('array_cases debe ser una lista de objetos')
        decisions = option.get('decisions')
        if not isinstance(decisions, dict) or not decisions:
            raise ValueError(f'array_cases[{idx}].decisions debe ser un objeto no vacío')
        array_values = {k:v for k,v in decisions.items() if isinstance(v, list)}
        if len(array_values) != 1 or any(not isinstance(v, list) or len(v) != 1 or
                                        not isinstance(v[0], dict) for v in array_values.values()):
            raise ValueError('array_cases solo admite una colección de un elemento objeto')
        if any(not isinstance(k, str) for k in decisions):
            raise ValueError('array_cases necesita claves UVL (string)')
        combined = dict(initial.decisions)
        conflict = [k for k,v in decisions.items() if k in combined and combined[k] != v]
        if conflict:
            raise ValueError(f'array_cases entra en conflicto con el caso base: {conflict}')
        combined.update(decisions)
        path = str(option.get('path') or option.get('point_feature'))
        feature = str(option.get('variant_feature'))
        value = option.get('value')
        cases.append(base.Case(
            case_id=f'{len(cases)+1:04d}_{base.safe_name(kind)}_'
                    f'{base.safe_name(path)}_{base.safe_name(value)}',
            kind=kind, api_version=version, root=root, path=path, value=value,
            decisions=combined, choice_feature=feature,
            source='FMVariationPoints:alternative:array_item',
        ))
    # Reenumerar todos los casos para que puedan ordenarse sin colisiones de nombre.
    for num, case in enumerate(cases, 1):
        suffix = case.case_id.split('_', 1)[-1]
        case.case_id = f'{num:05d}_{suffix}'
    return cases


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', required=True, type=Path)
    p.add_argument('--descriptions', required=True, type=Path)
    p.add_argument('--metadata', required=True, type=Path)
    p.add_argument('--mapper', required=True, type=Path)
    p.add_argument('--generator', type=Path, default=Path('main_k8s05.py'))
    p.add_argument('--uvl', type=Path, help='Solo obligatorio sin --plan')
    p.add_argument('--output-dir', type=Path, default=Path('experiments/uvl_variation/plan'))
    p.add_argument('--plan', action='store_true', help='Solo crear JSON parciales sin cargar UVL')
    p.add_argument('--limit', type=int, default=0, help='Número de casos, 0 = todos')
    p.add_argument('--variability-comments', action='store_true')
    args = p.parse_args(argv)
    try:
        if args.limit < 0:
            raise ValueError('--limit no puede ser negativo')
        if not args.plan and args.uvl is None:
            raise ValueError('Indica --uvl para generar los YAML, o usa --plan')
        mod = base.load_module('runner_v3_metadata_mapper', args.mapper)
        mapper = mod.MetadataMapper(str(args.descriptions), str(args.metadata))
        profile = base.load_json(args.profile)
        cases = cases_from_profile(profile, mapper)
        if args.limit:
            cases = cases[:args.limit]
        output_partials = args.output_dir / 'partial_configs'
        output_partials.mkdir(parents=True, exist_ok=True)
        rows = []
        for case in cases:
            row = {col: '' for col in base.CSV_COLUMNS}
            row.update(case_id=case.case_id, kind=case.kind,
                       api_version=case.api_version, variation_point=case.path,
                       requested_value=json.dumps(case.value, ensure_ascii=False),
                       choice_feature=case.choice_feature,
                       value_source=case.source,
                       status='skipped' if case.error else 'planned', error=case.error)
            if not case.error:
                path = output_partials / (case.case_id + '.json')
                path.write_text(json.dumps(case.decisions, ensure_ascii=False, indent=2) + '\n',
                                encoding='utf-8')
                row['partial_json'] = str(path)
            rows.append(row)
        csv_path = args.output_dir / 'generation_results.csv'
        base.save_csv(csv_path, rows)
        if not args.plan:
            base.generate(cases, rows, args)
        print(f'Casos: {len(rows)} | planificados: {sum(r["status"]=="planned" for r in rows)}'
              f' | generados: {sum(r["status"]=="generated" for r in rows)}'
              f' | fallidos: {sum(r["status"]=="failed" for r in rows)}'
              f' | omitidos: {sum(r["status"]=="skipped" for r in rows)}')
        print(f'JSON parciales: {output_partials}\nRegistro: {csv_path}')
        return 1 if any(row['status'] == 'failed' for row in rows) else 0
    except (OSError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())