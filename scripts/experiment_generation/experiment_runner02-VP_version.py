#!/usr/bin/env python3
"""Planificador de JSON parciales: variantes concretas o solo el punto de variación.

--case-mode variants (defecto): comportamiento idéntico al runner v4.
--case-mode points: un JSON por punto de variación alternative; no selecciona ningún
hijo concreto. Si el punto está en un array, conserva UNA instancia con el punto
activo pero sin su variante. No efectúa SAT ni genera YAML con --plan.

Para los YAML point-only con arrays se necesita generate_manifests_timed_v2.py,
que resuelve la variante elegida por SAT en el payload de la colección.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import experiment_runner as base
import experiment_runner02 as previous


def collapse_array_choice(value: Any, point: str, variant: str, seen: list[int]) -> Any:
    """Preserva la instancia del array pero sustituye {VP:{variant:true}} por VP:true."""
    if isinstance(value, list):
        return [collapse_array_choice(v, point, variant, seen) for v in value]
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key == variant:
                continue
            if key == point:
                seen[0] += 1
                result[key] = True
            else:
                result[key] = collapse_array_choice(child, point, variant, seen)
        return result
    return value


def cases_from_points(profile: dict[str, Any], mapper: Any) -> list[base.Case]:
    resources = profile.get('resources')
    if not isinstance(resources, list) or len(resources) != 1:
        raise ValueError('El modo points requiere un único recurso en auto_profile.json')
    resource = resources[0]
    kind, version = resource['kind'], resource['apiVersion']
    root = mapper.gvk_to_root.get((version, kind))
    if not root:
        raise ValueError(f'No existe la raíz UVL de {kind}/{version} en MetadataMapper')
    index = base.entries_for_root(mapper.serialization_entries, root)
    initial = base.make_cases({'resources': [{'kind': kind, 'apiVersion': version,
                                              'points': []}]}, mapper)[0]
    plans: dict[str, tuple[str, dict[str, Any], str]] = {}

    # Puntos escalares: recuperar la feature del padre de la relación alternative.
    for point in resource.get('points', []):
        path = point.get('path')
        if not isinstance(path, str) or not path:
            raise ValueError(f'Punto escalar inválido: {point}')
        parent = base.field_entry(index, path)
        if parent is None:
            raise ValueError(f'No se puede resolver la feature del punto {path}')
        feature_name = parent['feature']
        plans.setdefault(feature_name, (path, {feature_name: True}, 'alternative:point_only:scalar'))

    # Las alternatives de una sola colección están repetidas por variante en el
    # perfil original. Se agrupan por point_feature, manteniendo UNA instancia.
    for option in resource.get('array_cases', []):
        if option.get('relation_type') != 'alternative':
            continue
        point, variant = option.get('point_feature'), option.get('variant_feature')
        path, decisions = option.get('path'), option.get('decisions')
        if not isinstance(point, str) or not point or not isinstance(variant, str):
            raise ValueError('array_cases necesita point_feature y variant_feature')
        if not isinstance(decisions, dict) or not isinstance(path, str):
            raise ValueError(f'array_cases inválido para {point}')
        arrays = {k: v for k, v in decisions.items() if isinstance(v, list)}
        if len(arrays) != 1 or any(len(v) != 1 or not isinstance(v[0], dict)
                                  for v in arrays.values()):
            raise ValueError(f'Solo se admiten arrays de un objeto ({point})')
        array_name, array_value = next(iter(arrays.items()))
        seen = [0]
        instance = collapse_array_choice(copy.deepcopy(array_value), point, variant, seen)
        if seen[0] != 1:
            raise ValueError(f'No se encontró una única ocurrencia de {point} en {array_name}')
        # NO introducir ninguna feature variante en el nivel raíz.
        new = {point: True, array_name: instance}
        if point in plans:
            old_path, old, _ = plans[point]
            if old_path != path or old != new:
                raise ValueError(f'Instancias inconsistentes entre variantes de {point}')
        else:
            plans[point] = (path, new, 'alternative:point_only:array_item')

    # Mantener solo los feature_cases de tipo alternative si se incluyen en el perfil.
    for option in resource.get('feature_cases', []):
        if option.get('relation_type') != 'alternative':
            continue
        point = option.get('point_feature')
        if not isinstance(point, str) or not point:
            raise ValueError('feature_cases alternative sin point_feature')
        plans.setdefault(point, (str(option.get('path') or point), {point: True},
                                 'alternative:point_only:structural'))

    cases = [base.Case(case_id=f'00001_{base.safe_name(kind)}__base',
                       kind=kind, api_version=version, root=root, path='(base)',
                       value=None, decisions=dict(initial.decisions),
                       source='base')]
    for i, (name, (path, decision, source)) in enumerate(sorted(plans.items()), 2):
        combined = dict(initial.decisions)
        if any(k in combined and combined[k] != v for k, v in decision.items()):
            raise ValueError(f'Conflicto con el caso base en {name}')
        combined.update(decision)
        cases.append(base.Case(
            case_id=f'{i:05d}_{base.safe_name(kind)}__point__{base.safe_name(path)}',
            kind=kind, api_version=version, root=root, path=path,
            value='auto', decisions=combined, choice_feature=name, source=source))
    return cases


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', required=True, type=Path)
    p.add_argument('--descriptions', required=True, type=Path)
    p.add_argument('--metadata', required=True, type=Path)
    p.add_argument('--mapper', required=True, type=Path)
    p.add_argument('--output-dir', required=True, type=Path)
    p.add_argument('--case-mode', choices=('variants', 'points'), default='variants')
    p.add_argument('--plan', action='store_true',
                   help='Solo escribir JSON parciales (sin generar YAML)')
    p.add_argument('--limit', type=int, default=0)
    args = p.parse_args(argv)
    if not args.plan:
        p.error('Este planificador v5 se usa con --plan; genera YAML con generate_manifests_timed.py')
    if args.limit < 0:
        p.error('--limit no puede ser negativo')
    try:
        module = base.load_module('runner_v5_mapper', args.mapper)
        mapper = module.MetadataMapper(str(args.descriptions), str(args.metadata))
        profile = base.load_json(args.profile)
        cases = (previous.cases_from_profile(profile, mapper) if args.case_mode == 'variants'
                 else cases_from_points(profile, mapper))
        if args.limit:
            cases = cases[:args.limit]
        partials = args.output_dir / 'partial_configs'
        partials.mkdir(parents=True, exist_ok=True)
        rows = []
        for case in cases:
            row = {col: '' for col in base.CSV_COLUMNS}
            row.update(case_id=case.case_id, kind=case.kind,
                       api_version=case.api_version, variation_point=case.path,
                       requested_value=json.dumps(case.value, ensure_ascii=False),
                       choice_feature=case.choice_feature,
                       value_source=case.source, status='skipped' if case.error else 'planned',
                       error=case.error)
            if not case.error:
                out = partials / f'{case.case_id}.json'
                out.write_text(json.dumps(case.decisions, ensure_ascii=False, indent=2) + '\n',
                               encoding='utf-8')
                row['partial_json'] = str(out.resolve())
            rows.append(row)
        base.save_csv(args.output_dir/'generation_results.csv', rows)
        (args.output_dir/'plan_summary.json').write_text(json.dumps({
            'case_mode': args.case_mode,
            'total_cases': len(rows),
            'planned': sum(r['status']=='planned' for r in rows),
            'skipped': sum(r['status']=='skipped' for r in rows),
            'case_semantics': ('one case per VP parent, solver chooses alternative'
                               if args.case_mode == 'points'
                               else 'one case per explicitly specified value'),
        }, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
        print(f'Modo {args.case_mode} | JSON parciales: {len(rows)} | {partials}')
        return 0
    except (ValueError, OSError, TypeError, KeyError, ImportError, json.JSONDecodeError) as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())