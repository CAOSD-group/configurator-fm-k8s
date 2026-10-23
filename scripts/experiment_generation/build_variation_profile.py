#!/usr/bin/env python3
"""Descubre puntos de variación y genera un perfil reproducible para experiment_runner_v2.

No carga el UVL ni ejecuta SAT: lee los metadatos ya generados por convert04.py
mediante MetadataMapper, y valida cada propuesta mediante encode_property.

Por defecto solo genera casos para alternativas escalares con >=2 opciones
explícitas en el contrato de serialización. No inventa elementos de arrays,
claves de mapas ni valores numéricos sin una política de muestreo.
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from experiment_runner import (encode_property, entries_for_root,
                                  field_entry, load_module)

CATALOG_FIELDS = [
    'kind', 'api_version', 'root', 'path', 'schema_type', 'serialization_kind',
    'category', 'status', 'reason', 'candidates', 'accepted_values',
    'feature', 'num_options',
]


def scalar_value(value: Any) -> bool:
    return value is not None and type(value) in (str, int, float, bool)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def unique(values: list[Any]) -> list[Any]:
    out, seen = [], set()
    for value in values:
        marker = (type(value).__name__, canonical(value))
        if marker not in seen:
            seen.add(marker)
            out.append(value)
    return out


def find_sample_values(path: str, sample_map: dict[str, Any]) -> list[Any] | None:
    # Ordered rules. Exact path outranks globs; first glob wins.
    if path in sample_map:
        return sample_map[path]
    for pattern, values in sample_map.items():
        if fnmatch.fnmatchcase(path, pattern):
            return values
    return None


def is_nested_in_collection(path: str, index: dict[str, list[dict[str, Any]]]) -> bool:
    if '[]' in path or '{}' in path:
        return True
    for container_path, entries in index.items():
        if container_path and any(
            e.get('serializationRole') == 'property'
            and e.get('serializationKind') in ('array', 'map')
            for e in entries
        ) and (path.startswith(container_path + '.')
               or path.startswith(container_path + '[]')
               or path.startswith(container_path + '{}')):
            return True
    return False


def make_profile(mapper: Any, kinds: list[str], *,
                 includes: list[str] | None = None,
                 excludes: list[str] | None = None,
                 rules: dict[str, Any] | None = None,
                 max_points_per_resource: int = 12,
                 max_values_per_point: int = 6,
                 max_path_depth: int = 3
                 ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rules = rules or {}
    samples = rules.get('sample_values', {})
    if not isinstance(samples, dict):
        raise ValueError("'sample_values' debe ser un objeto {patrón: [valores]}")
    if any(not isinstance(values, list) for values in samples.values()):
        raise ValueError("Cada regla 'sample_values' debe contener una lista de valores")
    if max_points_per_resource < 1 or max_values_per_point < 2 or max_path_depth < 1:
        raise ValueError('max-points/depth deben ser positivos y max-values >= 2')
    includes = includes if includes is not None else ['spec.*']
    excludes = list(excludes or []) + list(rules.get('exclude_paths', []))
    categories = ('alternative', 'sampled_scalar')
    all_resources, rows = [], []

    known = list(mapper.gvk_to_root.items())
    targets = [((version, kind), root) for (version, kind), root in known
               if not kinds or kind in kinds]
    missing_kinds = set(kinds) - {gvk[1] for gvk, _ in targets}
    if missing_kinds:
        raise ValueError(f'Recursos desconocidos: {sorted(missing_kinds)}')

    for (api_version, kind), root in sorted(targets, key=lambda t: (t[0][1], t[0][0])):
        index = entries_for_root(mapper.serialization_entries, root)
        selected_points = []
        for path in sorted(index):
            if path in ('', 'apiVersion', 'kind', 'metadata.name'):
                continue
            parent = field_entry(index, path)
            if parent is None:
                continue  # Index contains duplicate/auxiliary path without one parent.
            schema = parent.get('schemaType', '')
            row: dict[str, Any] = {
                'kind': kind, 'api_version': api_version, 'root': root,
                'path': path, 'schema_type': schema,
                'serialization_kind': parent.get('serializationKind', ''),
                'category': '', 'status': 'excluded', 'reason': '',
                'candidates': '[]', 'accepted_values': '[]',
                'feature': parent.get('feature', ''), 'num_options': 0,
            }
            options = [e for e in index[path]
                       if e.get('synthetic') is True
                       and e.get('serializationRole') == 'value'
                       and 'configValue' in e]
            explicit = unique([e['configValue'] for e in options
                               if scalar_value(e.get('configValue'))])
            sampled = find_sample_values(path, samples)
            if sampled is not None:
                candidates = unique(sampled)
                row['category'] = 'sampled_scalar'
            else:
                candidates = explicit
                row['category'] = 'alternative' if len(explicit) >= 2 else 'scalar_review'
            row['candidates'] = canonical(candidates)
            row['num_options'] = len(candidates)

            if includes and not any(fnmatch.fnmatchcase(path, pat) for pat in includes):
                row['reason'] = 'outside_include_scope'
            elif any(fnmatch.fnmatchcase(path, pat) for pat in excludes):
                row['reason'] = 'excluded_by_pattern'
            elif is_nested_in_collection(path, index):
                row['reason'] = 'nested_in_array_or_map'
            elif len(path.split('.')) > max_path_depth:
                row['reason'] = 'path_too_deep_for_initial_pilot'
            elif parent.get('serializationKind') in ('array', 'map', 'object', 'union'):
                row['reason'] = 'complex_structure'
            elif row['category'] not in categories:
                row['reason'] = 'no_explicit_alternatives_or_sample_rule'
            elif len(candidates) < 2:
                row['reason'] = 'fewer_than_two_values'
            else:
                accepted, rejected = [], []
                for value in candidates:
                    try:
                        encode_property(index, path, value)
                        accepted.append(value)
                    except (ValueError, TypeError) as exc:
                        rejected.append(f'{value!r}: {exc}')
                row['accepted_values'] = canonical(accepted)
                if len(accepted) < 2:
                    row['reason'] = 'not_enough_encodable_values: ' + '; '.join(rejected)[:220]
                elif len(selected_points) >= max_points_per_resource:
                    row['reason'] = 'max_points_per_resource'
                else:
                    taken = accepted[:max_values_per_point]
                    if len(taken) < 2:
                        row['reason'] = 'fewer_than_two_values_after_limit'
                    else:
                        selected_points.append({'path': path, 'values': taken})
                        row['status'] = 'included'
                        row['reason'] = ('truncated_to_max_values' if len(accepted) > len(taken)
                                         else ('some_values_rejected: ' + '; '.join(rejected)[:200]
                                               if rejected else ''))
            rows.append(row)
        # Runner will create one base case plus all selected variant cases.
        if selected_points:
            all_resources.append({'kind': kind, 'apiVersion': api_version,
                                  'points': selected_points})
    return {'resources': all_resources}, rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--descriptions', required=True, type=Path)
    p.add_argument('--metadata', required=True, type=Path)
    p.add_argument('--mapper', type=Path, default=Path('metadata_parser01.py'))
    p.add_argument('--kinds', nargs='*', default=['Deployment', 'Service'],
                   help='Tipos Kubernetes que queremos estudiar')
    p.add_argument('--include', nargs='*', default=['spec.*'],
                   help='Patrones de configPath; defecto spec.*')
    p.add_argument('--exclude', nargs='*', default=[], help='Patrones excluidos')
    p.add_argument('--rules', type=Path, help='JSON con sample_values y exclude_paths')
    p.add_argument('--max-points-per-resource', type=int, default=12)
    p.add_argument('--max-values-per-point', type=int, default=6)
    p.add_argument('--max-path-depth', type=int, default=3,
                   help='Máximo número de segmentos del configPath en el piloto inicial')
    p.add_argument('--output-dir', type=Path, default=Path('experiments/auto_plan'))
    args = p.parse_args(argv)

    try:
        metadata_mapper = load_module('auto_profile_metadata_mapper', args.mapper)
        mapper = metadata_mapper.MetadataMapper(str(args.descriptions), str(args.metadata))
        rules = json.loads(args.rules.read_text(encoding='utf-8')) if args.rules else {}
        profile, catalog = make_profile(
            mapper, args.kinds, includes=args.include, excludes=args.exclude,
            rules=rules, max_points_per_resource=args.max_points_per_resource,
            max_values_per_point=args.max_values_per_point,
            max_path_depth=args.max_path_depth,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        profile_path = args.output_dir / 'auto_profile.json'
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + '\n',
                                encoding='utf-8')
        catalog_path = args.output_dir / 'variation_catalog.csv'
        with catalog_path.open('w', encoding='utf-8-sig', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=CATALOG_FIELDS)
            writer.writeheader()
            writer.writerows(catalog)
        n_variations = sum(len(p['values']) for r in profile['resources']
                           for p in r['points'])
        n_base = len(profile['resources'])
        print(f'Recursos: {n_base} | puntos: {sum(len(r["points"]) for r in profile["resources"])}'
              f' | casos planificables: {n_variations+n_base} (incluye {n_base} bases)')
        print(f'Perfil: {profile_path}\nCatálogo y exclusiones: {catalog_path}')
        if not profile['resources']:
            print('AVISO: no hay puntos admisibles; revisa el catálogo o los filtros.')
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
