#!/usr/bin/env python3
"""E2-B: descubre TODOS los grupos alternative con FlamaPy FMVariationPoints.

V2: plans Quantity-valued dynamic maps with a representative map key; the
selected asString/asNumber value is materialized by the compatible V6 runner.

Genera un catálogo exhaustivo, un perfil de puntos (NO compatible con --profile
of experiment_runner_v5.py), y JSON parciales únicamente para puntos que
se pueden situar razonablemente en el YAML. No ejecuta SAT ni genera YAML.

No deduce obligatoriedad Kubernetes ni inventa variantes o valores.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


COLUMNS = [
    'case_id', 'kind', 'api_version', 'variation_point', 'requested_value',
    'choice_feature', 'value_source', 'status', 'error', 'partial_json',
    'point_feature', 'relation_type', 'n_variants', 'variant_features',
    'config_path', 'n_array_ancestors', 'plan_type', 'reason',
]


def attribute(feature: Any, name: str) -> Any:
    for item in feature.get_attributes():
        if item.name == name:
            return item.get_default_value()
    return None


def ancestors_from_root(feature: Any, root: Any) -> list[Any] | None:
    nodes = []
    while feature is not None:
        nodes.append(feature)
        if feature is root or feature.name == root.name:
            return list(reversed(nodes))
        feature = feature.get_parent()
    return None


def feature_props(node: Any, by_feature: dict[str, list[dict]]) -> dict[str, Any]:
    props = dict()
    for key in ('configKey', 'serializationKind', 'serializationRole', 'schema_type', 'schemaItemsType', 'schemaAdditionalPropertiesRef'):
        value = attribute(node, key)
        if value is not None:
            props[key] = value
    for entry in by_feature.get(node.name, []):
        if entry.get('serializationRole') in ('property', 'structural', 'arrayItem', 'mapKey', 'mapValue'):
            for target, source in (
                ('configKey', 'configKey'), ('serializationKind', 'serializationKind'),
                ('serializationRole', 'serializationRole'), ('schema_type', 'schemaType'),
                ('schemaItemsType', 'schemaItemsType'),
                ('schemaAdditionalPropertiesRef', 'schemaAdditionalPropertiesRef'),
            ):
                if not props.get(target) and entry.get(source):
                    props[target] = entry[source]
    return props


def choose_path(point: Any, children: list[Any], chain: list[Any],
                by_feature: dict[str, list[dict]]) -> str:
    # The actual alternative parent is preferred over a child's value metadata.
    for node in [point, *children, *reversed(chain[:-1])]:
        candidates = sorted({e['configPath'] for e in by_feature.get(node.name, [])
                             if e.get('configPath') and
                             e.get('serializationRole') in ('property', 'value', 'structural')})
        if len(candidates) == 1:
            return candidates[0]
    return ''


def safe_id(index: int, kind: str, name: str) -> str:
    short = re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_')[:38]
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]
    return f'{index:05d}_{kind}_point_{short}_{digest}'


def plan_point(point: Any, chain: list[Any], by_feature: dict[str, list[dict]],
               path: str, *, include_status: bool = False, map_key: str = 'cpu') -> tuple[dict[str, Any] | None, str, str]:
    """Return (decisions, classification, reason) without choosing any alternative.

    Every nested object-array is instantiated with one object ONLY where needed
    to address the selected point. Do not instantiate scalar-array items.
    """
    props = [feature_props(node, by_feature) for node in chain]
    # Status is server-managed and not a creation case by default.
    if not include_status and (path == 'status' or path.startswith('status.') or
                               any(p.get('configKey') == 'status' for p in props[1:])):
        return None, 'blocked', 'read_only_status'
    # A single dynamic Quantity map is addressable with one representative
    # key. Do NOT choose the alternative value here: the SAT/MaxSAT result does
    # that at render time. Other maps remain blocked rather than inventing keys.
    map_nodes = [i for i, p in enumerate(props[1:], 1)
                 if p.get('serializationKind') == 'map' or
                    p.get('serializationRole') in ('mapKey', 'mapValue')]
    map_point = bool(map_nodes and map_nodes == [len(chain)-1] and
                     props[-1].get('serializationKind') == 'map')
    key_feature = None
    chosen_map_key = ('storage' if 'volumeClaimTemplate' in path or
                      chain[0].name.endswith('_PersistentVolumeClaim') else map_key)
    if map_point:
        ref = str(props[-1].get('schemaAdditionalPropertiesRef') or '').lower()
        keys = [c for c in point.get_children()
                if feature_props(c, by_feature).get('serializationRole') == 'mapKey']
        alternatives = [r for r in point.get_relations() if r.is_alternative()]
        values = (list(alternatives[0].children) if len(alternatives) == 1 else [])
        if re.search(r'(?:[./])quantity$', ref) and len(keys) == 1 and values and all(
                feature_props(c, by_feature).get('serializationRole') == 'mapValue'
                for c in values):
            key_feature = keys[0]
    if map_nodes and key_feature is None:
        return None, 'blocked', 'dynamic_map_requires_key'
    if not path:
        return None, 'blocked', 'no_unambiguous_serialization_path'
    if not map_key or not re.fullmatch(r'[a-z][a-z0-9._/-]*', map_key):
        raise ValueError(f'Invalid synthetic map key: {map_key!r}')
    if any(p.get('serializationRole') == 'arrayItem' for p in props[1:]):
        return None, 'blocked', 'scalar_array_item_requires_concrete_occurrence'
    if any(p.get('serializationKind') == 'array' and
           str(p.get('schemaItemsType', '')).lower() in
           ('string', 'integer', 'number', 'boolean') for p in props[1:-1]):
        return None, 'blocked', 'scalar_array_ancestor_requires_value'

    # Exclude the resource root itself from the structural path.
    array_indices = [i for i in range(1, len(chain))
                     if props[i].get('serializationKind') == 'array']
    if not array_indices:
        if key_feature is not None:
            # A map row containing ONLY a concrete key. The alternative value
            # remains unspecified until SAT chooses its mapValue child.
            return {point.name: [{key_feature.name: chosen_map_key}]}, 'dynamic_quantity_map_point', ''
        return {point.name: True}, 'scalar_or_object_point', ''
    first = array_indices[0]
    if first == len(chain) - 1:
        # Choosing the collection itself does not specify a concrete element.
        return None, 'blocked', 'array_point_requires_item_instance'

    # Point under a collection: preserve a *single* concrete occurrence as a
    # path to the point. Every array beyond the first is also represented by
    # one item, not a claimed global cardinality constraint.
    inside: dict[str, Any] = (
        {point.name: [{key_feature.name: chosen_map_key}]} if key_feature is not None
        else {point.name: True}
    )
    for idx in range(len(chain) - 2, first, -1):
        node, prop = chain[idx], props[idx]
        if prop.get('serializationKind') == 'array':
            inside = {node.name: [inside]}
        else:
            inside = {node.name: inside}
    decisions = {point.name: True, chain[first].name: [inside]}
    typ = ('dynamic_quantity_map_in_array' if key_feature is not None
           else 'single_object_array_point' if len(array_indices) == 1
           else 'nested_object_arrays_experimental')
    return decisions, typ, ''


def plan_all(fm: Any, vps: list[Any], metadata: dict[str, Any], *, root_name: str,
             kind: str, api_version: str, resource_name: str,
             include_status: bool = False, map_key: str = 'cpu') -> tuple[dict, list[dict], dict, dict]:
    features = {node.name: node for node in fm.get_features()}
    root = features.get(root_name)
    if root is None:
        raise ValueError(f'No existe root feature {root_name!r}')
    if not isinstance(metadata, dict) or not isinstance(metadata.get('features'), list):
        raise ValueError('serialization_metadata debe contener features[]')
    by_feature: dict[str, list[dict]] = defaultdict(list)
    for entry in metadata['features']:
        if isinstance(entry, dict) and entry.get('feature') in features:
            by_feature[entry['feature']].append(entry)

    base_fields = {}
    for path, value in [('apiVersion', api_version), ('kind', kind), ('metadata.name', resource_name)]:
        candidates = [entry['feature'] for entry in metadata['features']
                      if isinstance(entry, dict) and entry.get('configPath') == path
                      and entry.get('serializationRole') == 'property'
                      and entry.get('feature') in features
                      and ancestors_from_root(features[entry['feature']], root)]
        candidates = sorted(set(candidates))
        if len(candidates) != 1:
            # V3 schema metadata may have duplicates. Ambiguity is dangerous.
            raise ValueError(f'La ruta base {path!r} tiene {len(candidates)} features '
                             f'en {root_name}: {candidates[:5]}')
        base_fields[candidates[0]] = value

    all_groups = []
    for vp in vps:
        chain = ancestors_from_root(vp, root)
        if chain is None:
            continue
        for group_no, relation in enumerate(vp.get_relations(), 1):
            if relation.is_alternative():
                all_groups.append((vp, chain, group_no, list(relation.children)))
    all_groups.sort(key=lambda x: (x[0].name, x[2]))
    groups_per_parent = Counter(g[0].name for g in all_groups)

    csv_rows: list[dict] = []
    partials: dict[str, dict] = {}
    profile = {'format': 'e2b_fmvariationpoints_all_alternatives_v1',
               'root_feature': root_name, 'gvk': {'apiVersion': api_version, 'kind': kind},
               'base_decisions': base_fields, 'points': [],
               'synthetic_map_key': map_key}
    base_id = f'00001_{kind}__base'
    csv_rows.append({'case_id': base_id, 'kind': kind, 'api_version': api_version,
                     'variation_point': '(base)', 'requested_value': 'null',
                     'choice_feature': '', 'value_source': 'base', 'status': 'planned',
                     'error': '', 'partial_json': '', 'point_feature': '',
                     'relation_type': '', 'n_variants': 0, 'variant_features': '[]',
                     'config_path': '', 'n_array_ancestors': 0, 'plan_type': 'base', 'reason': ''})
    partials[base_id] = base_fields.copy()

    for idx, (vp, chain, group_no, children) in enumerate(all_groups, start=2):
        path = choose_path(vp, children, chain, by_feature)
        array_count = sum(feature_props(node, by_feature).get('serializationKind') == 'array'
                          for node in chain[1:])
        decisions, plan_type, reason = plan_point(
            vp, chain, by_feature, path, include_status=include_status, map_key=map_key)
        if groups_per_parent[vp.name] > 1:
            # VP:true cannot distinguish which relation is being tested.
            decisions, plan_type, reason = None, 'blocked', 'multiple_alternative_groups_on_same_parent'
        case_id = safe_id(idx, kind, vp.name + f'#g{group_no}')
        if decisions is not None:
            collision = [name for name, val in decisions.items()
                         if name in base_fields and base_fields[name] != val]
            if collision:
                decisions = None
                plan_type, reason = 'blocked', f'conflicting_base_decision:{collision}'
        status = 'planned' if decisions is not None else 'blocked'
        if decisions is not None:
            partials[case_id] = {**base_fields, **decisions}
        profile['points'].append({
            'case_id': case_id, 'point_feature': vp.name,
            'relation_index': group_no, 'relation_type': 'alternative',
            'variants': [c.name for c in children], 'config_path': path,
            'n_array_ancestors': array_count, 'plan_type': plan_type,
            'status': status, 'reason': reason,
            'decisions': decisions if decisions is not None else {},
        })
        csv_rows.append({
            'case_id': case_id, 'kind': kind, 'api_version': api_version,
            'variation_point': path or vp.name, 'requested_value': '"auto"',
            'choice_feature': vp.name, 'value_source': 'FMVariationPoints:alternative:point_only',
            'status': status, 'error': reason, 'partial_json': '',
            'point_feature': vp.name, 'relation_type': 'alternative',
            'n_variants': len(children),
            'variant_features': json.dumps([c.name for c in children], ensure_ascii=False),
            'config_path': path, 'n_array_ancestors': array_count,
            'plan_type': plan_type, 'reason': reason,
        })
    stats = {
        'resource_root': root_name, 'kind': kind, 'api_version': api_version,
        'alternative_groups_discovered': len(all_groups),
        'alternative_parent_features': len({g[0].name for g in all_groups}),
        'alternative_children_total': sum(len(g[3]) for g in all_groups),
        'cases_total_including_base': len(csv_rows),
        'cases_planned': sum(r['status'] == 'planned' for r in csv_rows),
        'cases_blocked': sum(r['status'] == 'blocked' for r in csv_rows),
        'groups_by_plan_type': dict(Counter(p['plan_type'] for p in profile['points'])),
        'blocked_reasons': dict(Counter(r['reason'] for r in csv_rows if r['status'] == 'blocked')),
        'semantics': ('Points are selected without selecting a variant. '
                      'Single item scaffolds localize a point in object arrays; '
                      'SAT and actual YAML materialization still require evaluation.'),
    }
    return profile, csv_rows, stats, partials


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uvl', required=True, type=Path)
    parser.add_argument('--metadata', required=True, type=Path)
    parser.add_argument('--root-feature', required=True)
    parser.add_argument('--kind', default='Deployment')
    parser.add_argument('--api-version', default='apps/v1')
    parser.add_argument('--resource-name', default='fm-pilot-deployment')
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--expected-groups', type=int, default=0,
                        help='If nonzero, require exactly this number of alternative relations')
    parser.add_argument('--include-status', action='store_true')
    parser.add_argument('--map-key', default='cpu',
                        help='Representative key for Quantity maps (default: cpu)')
    args = parser.parse_args(argv)
    try:
        # Delay import so pure tests do not require a local FlamaPy installation.
        from flamapy.metamodels.fm_metamodel.transformations import UVLReader
        from flamapy.metamodels.fm_metamodel.operations import FMVariationPoints
        print('Cargando UVL...', flush=True)
        fm = UVLReader(str(args.uvl)).transform()
        print('Ejecutando FMVariationPoints...', flush=True)
        vps = FMVariationPoints().execute(fm).get_result()
        metadata = json.loads(args.metadata.read_text(encoding='utf-8-sig'))
        profile, rows, summary, partials = plan_all(
            fm, vps, metadata, root_name=args.root_feature,
            kind=args.kind, api_version=args.api_version,
            resource_name=args.resource_name, include_status=args.include_status,
            map_key=args.map_key)
        if args.expected_groups and summary['alternative_groups_discovered'] != args.expected_groups:
            raise ValueError(f'El modelo contiene {summary["alternative_groups_discovered"]} '
                             f'grupos alternative, no los {args.expected_groups} esperados')
        output = args.output_dir.resolve()
        if output.exists() and any(output.iterdir()):
            raise ValueError(f'El directorio {output} no está vacío; usa uno nuevo')
        partial_dir = output/'partial_configs'
        partial_dir.mkdir(parents=True, exist_ok=True)
        for case_id, decision in partials.items():
            target = partial_dir/f'{case_id}.json'
            target.write_text(json.dumps(decision, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
        for row in rows:
            if row['status'] == 'planned':
                row['partial_json'] = str(partial_dir/f'{row["case_id"]}.json')
        (output/'auto_profile_e2b_all_points.json').write_text(
            json.dumps(profile, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        (output/'discovery_summary.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        with (output/'generation_results.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        print(f'Grupos alternative descubiertos: {summary["alternative_groups_discovered"]}')
        print(f'Casos planificados (incluido base): {summary["cases_planned"]}')
        print(f'Grupos bloqueados: {summary["cases_blocked"]}')
        print(f'Perfil y plan: {output}')
        return 0
    except (ImportError, ValueError, OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())