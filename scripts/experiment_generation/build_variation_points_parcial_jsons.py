#!/usr/bin/env python3
"""Descubre puntos de variación REALES con FlamaPy/FMVariationPoints bajo --root-feature.

Exporta catálogo completo de relaciones no obligatorias y un perfil OFAT consumible
por experiment_runner.py. Permite --only-alternatives y --materialize-single-arrays para variantes dentro de arrays. No ejecuta SAT.
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from experiment_runner import (encode_property, entries_for_root, field_entry,
                                  load_module, scalar_type_matches)
from build_variation_profile import is_nested_in_collection, unique, find_sample_values

COLS = [
    'root_feature', 'resource_root', 'kind', 'api_version', 'point_feature',
    'relation_type', 'variant_feature', 'variant_value', 'config_path',
    'schema_type', 'serialization_kind', 'status', 'reason',
]


def attr(feature: Any, name: str) -> Any:
    for a in feature.get_attributes():
        if a.name == name:
            return a.get_default_value()
    return None


def reltype(relation: Any) -> str:
    for method, label in [
        ('is_mandatory', 'mandatory'), ('is_optional', 'optional'),
        ('is_alternative', 'alternative'), ('is_or', 'or'),
        ('is_cardinality', 'cardinality'),
    ]:
        fn = getattr(relation, method, None)
        if callable(fn) and fn():
            return label
    return 'non_mandatory_other'


def is_descendant(node: Any, ancestor: Any) -> bool:
    while node is not None:
        if node is ancestor or node.name == ancestor.name:
            return True
        node = node.get_parent()
    return False


def metadata_for_feature(entries: dict[str, list[dict[str, Any]]], feat: Any) -> list[dict[str, Any]]:
    return entries.get(feat.name, [])


def best_path(vp: Any, variant: Any,
              by_feature: dict[str, list[dict[str, Any]]],
              index: dict[str, list[dict[str, Any]]]) -> str | None:
    """Preferir el path de variante o padre, sin inventar rutas cuando no hay metadatos."""
    for feat in (variant, vp):
        items = metadata_for_feature(by_feature, feat)
        for role in ('value', 'property', 'structural'):
            paths = sorted({item['configPath'] for item in items
                            if item.get('serializationRole') == role
                            and isinstance(item.get('configPath'), str)
                            and item['configPath']})
            if len(paths) == 1:
                return paths[0]
    # Los puntos abstractos pueden anclarse en la propiedad de un ancestro.
    ancestor = vp.get_parent()
    while ancestor is not None:
        items = metadata_for_feature(by_feature, ancestor)
        paths = sorted({item['configPath'] for item in items
                        if item.get('serializationRole') == 'property'
                        and isinstance(item.get('configPath'), str)
                        and item['configPath']})
        if len(paths) == 1 and field_entry(index, paths[0]):
            return paths[0]
        ancestor = ancestor.get_parent()
    return None


def locate_resource(fm: Any, chosen: Any, mapper: Any) -> tuple[str, str, str]:
    roots = {root: (version, kind)
             for (version, kind), root in mapper.gvk_to_root.items()}
    current = chosen
    while current is not None:
        if current.name in roots:
            version, kind = roots[current.name]
            return current.name, version, kind
        current = current.get_parent()
    raise ValueError('La feature indicada no pertenece a un recurso GVK reconocido por MetadataMapper. '
                     'Selecciona una feature interna de un recurso como Deployment/Pod/Service.')



def plan_single_array_item(fm: Any, index: dict[str, list[dict[str, Any]]],
                           field_feature: Any, variant: Any,
                           chosen_decisions: dict[str, Any], root: Any) -> tuple[dict[str, Any] | None, str]:
    """One single array-of-objects occurrence for a chosen enum variant.

    Field/variant choices are included as top-level SAT assumptions. An array
    occurrence, whose cardinality SAT does not model, is preserved as structured
    input for main_k8s05. No names, paths, or missing mandatory scalar values
    are fabricated. Dynamic maps and multiple nested arrays remain unsupported.
    """
    ancestors = []
    node = field_feature
    while node is not None and node.name != root.name:
        ancestors.append(node)
        node = node.get_parent()
    if node is None:
        return None, 'feature_outside_resource'
    array_positions = []
    for pos, feat in enumerate(ancestors):
        entry = next((x for xlist in index.values() for x in xlist
                      if x.get('feature') == feat.name
                      and x.get('serializationRole') in ('property', 'structural')
                      and not x.get('synthetic', False)), None)
        kind = (entry or {}).get('serializationKind')
        if kind == 'map':
            return None, 'map_needs_dynamic_key'
        if kind == 'array':
            array_positions.append(pos)
    if len(array_positions) != 1:
        return None, ('multiple_nested_arrays_not_supported' if array_positions
                      else 'array_ancestor_not_found')
    array_pos = array_positions[0]
    if array_pos == 0:
        return None, 'alternative_array_value_not_object'

    # The array item describes descendants using exact UVL feature names.
    # fsType -> {specific_value: True}; vsphereVolume -> {fsType: ...}
    value: dict[str, Any] = {variant.name: True}
    for feat in ancestors[:array_pos]:
        value = {feat.name: value}
    array_feature = ancestors[array_pos]
    result = dict(chosen_decisions)
    result[array_feature.name] = [value]
    return result, ''

def make_uvl_profile(fm: Any, mapper: Any, selected_root: Any, all_vps: list[Any],
                     *, rules: dict[str, Any] | None = None, include_status: bool = False,
                     max_cases: int = 0, only_alternatives: bool = False,
                     materialize_single_arrays: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Pura para pruebas: el modelo y los variation points se reciben ya cargados.

    - CSV: TODOS los grupos no obligatorios del subárbol, incluidos los no automatizables.
    - Perfil: alternativas con configValue codificable, booleanos opcionales y muestreo escalar.
    - Con only_alternatives: únicamente alternativas enumeradas en el perfil.
    - Sin validación SAT en fase plan; se realiza al generar YAML en el runner.
    """
    rules = rules or {}
    samples = {} if only_alternatives else rules.get('sample_values', {})
    if not isinstance(samples, dict) or any(not isinstance(v, list) for v in samples.values()):
        raise ValueError('Reglas incorrectas: sample_values debe ser un objeto de listas')
    exclude_patterns = rules.get('exclude_paths', [])
    if not isinstance(exclude_patterns, list):
        raise ValueError('Reglas incorrectas: exclude_paths debe ser una lista')
    root, version, kind = locate_resource(fm, selected_root, mapper)
    index = entries_for_root(mapper.serialization_entries, root)
    by_feature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entries in index.values():
        for ent in entries:
            by_feature[ent['feature']].append(ent)

    # Indexar una sola vez: en un FM de ~81.000 features, consultar get_feature_by_name
    # para cada alternativa puede convertir un plan rápido en uno cuadrático.
    try:
        features_by_name = {feature.name: feature for feature in fm.get_features()}
    except AttributeError:
        features_by_name = dict(fm.features)  # solo dobles sintéticos de prueba

    rows: list[dict[str, Any]] = []
    grouped_options: dict[str, list[Any]] = defaultdict(list)
    feature_cases: list[dict[str, Any]] = []
    array_cases: list[dict[str, Any]] = []
    seen_array_cases: set[str] = set()
    seen_direct: set[tuple[str, str]] = set()
    inspected_vps = 0
    total_candidates = 0
    relation_group_counts: Counter[str] = Counter()
    variation_point_sets: dict[str, set[str]] = defaultdict(set)
    variant_counts_by_type: Counter[str] = Counter()
    # No depende de que los nombres sigan una determinada convención de prefijos.
    for vp in sorted(all_vps, key=lambda f: f.name):
        if not is_descendant(vp, selected_root):
            continue
        inspected_vps += 1
        for relation in vp.get_relations():
            relation_kind = reltype(relation)
            if relation_kind == 'mandatory':
                continue
            relation_group_counts[relation_kind] += 1
            variation_point_sets[relation_kind].add(vp.name)
            variant_counts_by_type[relation_kind] += len(relation.children)
            for variant in sorted(relation.children, key=lambda f: f.name):
                total_candidates += 1
                path = best_path(vp, variant, by_feature, index)
                parent = field_entry(index, path) if path else None
                raw_value = attr(variant, 'configValue')
                if raw_value is None:
                    for ent in metadata_for_feature(by_feature, variant):
                        if ent.get('serializationRole') == 'value' and 'configValue' in ent:
                            raw_value = ent['configValue']
                            break
                row = {
                    'root_feature': selected_root.name, 'resource_root': root,
                    'kind': kind, 'api_version': version,
                    'point_feature': vp.name, 'relation_type': relation_kind,
                    'variant_feature': variant.name, 'variant_value': (
                        '' if raw_value is None else json.dumps(raw_value, ensure_ascii=False)),
                    'config_path': path or '',
                    'schema_type': parent.get('schemaType', '') if parent else '',
                    'serialization_kind': parent.get('serializationKind', '') if parent else '',
                    'status': 'excluded', 'reason': '',
                }
                if not path or parent is None:
                    row['reason'] = 'unmapped_or_ambiguous_path'
                elif not include_status and (path == 'status' or path.startswith('status.')):
                    row['reason'] = 'read_only_status'
                elif any(fnmatch.fnmatchcase(path, pat) for pat in exclude_patterns):
                    row['reason'] = 'excluded_by_rule'
                elif is_nested_in_collection(path, index):
                    if relation_kind != 'alternative' or not materialize_single_arrays:
                        row['reason'] = 'requires_collection_instances'
                    elif raw_value is None:
                        row['reason'] = 'alternative_without_concrete_scalar_value'
                    elif parent.get('serializationKind') in ('array', 'map', 'object', 'union'):
                        row['reason'] = 'complex_or_non_scalar'
                    else:
                        try:
                            chosen_decisions, chosen, _ = encode_property(index, path, raw_value)
                            chosen_feat = features_by_name.get(chosen)
                            if chosen_feat is None or not is_descendant(chosen_feat, variant):
                                row['reason'] = 'encoded_variant_does_not_match_uvl_choice'
                            else:
                                field_feature = features_by_name.get(parent['feature'])
                                if field_feature is None:
                                    row['reason'] = 'missing_property_feature'
                                else:
                                    case_decisions, failure = plan_single_array_item(
                                        fm, index, field_feature, variant,
                                        chosen_decisions, features_by_name[root]
                                    )
                                    if failure:
                                        row['reason'] = failure
                                    elif chosen not in seen_array_cases:
                                        seen_array_cases.add(chosen)
                                        array_cases.append({
                                            'point_feature': vp.name,
                                            'relation_type': relation_kind,
                                            'variant_feature': chosen,
                                            'path': path,
                                            'value': raw_value,
                                            'decisions': case_decisions,
                                        })
                                        row['status'] = 'included'
                                        row['reason'] = 'enumerated_alternative_single_array_item'
                                    else:
                                        row['reason'] = 'duplicate_array_alternative'
                        except (ValueError, TypeError) as exc:
                            row['reason'] = f'cannot_encode: {exc}'[:220]
                elif parent.get('serializationKind') in ('array', 'map', 'object', 'union'):
                    row['reason'] = 'complex_or_non_scalar'
                elif relation_kind == 'alternative':
                    if raw_value is None:
                        row['reason'] = 'alternative_without_concrete_scalar_value'
                    else:
                        try:
                            _, chosen, _ = encode_property(index, path, raw_value)
                            chosen_feat = features_by_name.get(chosen)
                            if chosen_feat is None or not is_descendant(chosen_feat, variant):
                                row['reason'] = 'encoded_variant_does_not_match_uvl_choice'
                            else:
                                row['status'] = 'included'
                                row['reason'] = 'enumerated_alternative'
                                grouped_options[path].append(raw_value)
                        except (ValueError, TypeError) as exc:
                            row['reason'] = f'cannot_encode: {exc}'[:220]
                elif relation_kind in ('optional', 'or', 'cardinality', 'non_mandatory_other') and only_alternatives:
                    row['reason'] = 'filtered_by_only_alternatives'
                elif relation_kind in ('optional', 'or', 'cardinality', 'non_mandatory_other'):
                    # Activación/desactivación de flag booleano YAML directamente serializable.
                    if (parent.get('feature') == variant.name
                            and parent.get('schemaType') == 'boolean'
                            and parent.get('serializationKind') not in
                            ('array', 'map', 'object', 'union')
                            and variant.is_boolean()):
                        choices = [True, False] if relation_kind == 'optional' else [True]
                        for value in choices:
                            ident = (variant.name, json.dumps(value))
                            if ident in seen_direct:
                                continue
                            seen_direct.add(ident)
                            feature_cases.append({
                                'point_feature': vp.name, 'relation_type': relation_kind,
                                'variant_feature': variant.name, 'path': path,
                                'value': value, 'decisions': {variant.name: value},
                            })
                        row['status'] = 'included'
                        row['reason'] = 'boolean_feature_toggle'
                    else:
                        row['reason'] = 'non_boolean_or_structural_selection_needs_value_or_instances'
                else:
                    row['reason'] = 'relation_type_not_supported'
                rows.append(row)

    # Muestras de números/cadenas: parámetros de valor, no necesariamente VP estructurales.
    sampled_paths = []
    directly_covered_paths = {c['path'] for c in feature_cases}
    for path in sorted(index):
        values = find_sample_values(path, samples)
        if values is None:
            continue
        parent = field_entry(index, path)
        if parent is None or path in grouped_options or path in directly_covered_paths:
            continue
        pf = features_by_name.get(parent['feature'])
        if pf is None or not is_descendant(pf, selected_root):
            continue
        if ((not include_status and (path == 'status' or path.startswith('status.')))
                or any(fnmatch.fnmatchcase(path, pat) for pat in exclude_patterns)
                or is_nested_in_collection(path, index)):
            continue
        accepted = []
        for val in unique(values):
            if not scalar_type_matches(val, parent.get('schemaType')):
                continue
            try:
                encode_property(index, path, val)
                accepted.append(val)
            except (TypeError, ValueError):
                pass
        if accepted:
            grouped_options[path].extend(accepted)
            sampled_paths.append(path)
    points = [{'path': path, 'values': unique(vals)}
              for path, vals in sorted(grouped_options.items()) if vals]
    feature_cases.sort(key=lambda c: (c['path'], c['variant_feature'], str(c['value'])))
    array_cases.sort(key=lambda c: (c['path'], c['variant_feature'], str(c['value'])))
    if max_cases < 0:
        raise ValueError('--max-cases debe ser >= 0 (0 = sin límite)')
    if max_cases:
        # Casos incluidos en la cuota: primero valores enumerados, después toggles.
        left = max_cases - 1   # reservar un caso base
        limited_points = []
        for pt in points:
            taken = pt['values'][:max(0, left)]
            if taken:
                limited_points.append({'path': pt['path'], 'values': taken})
                left -= len(taken)
        points = limited_points
        feature_cases = feature_cases[:max(0, left)]
        left -= len(feature_cases)
        array_cases = array_cases[:max(0, left)]

    profile = {'resources': [{
        'kind': kind, 'apiVersion': version, 'points': points,
        'feature_cases': feature_cases, 'array_cases': array_cases,
    }]}
    status_counts = Counter(r['reason'] for r in rows)
    alternative_counts = Counter(r['reason'] for r in rows
                                 if r['relation_type'] == 'alternative')
    stats = {
        'root_feature': selected_root.name, 'manifest_root': root,
        'api_version': version, 'kind': kind,
        'total_variation_points': inspected_vps,
        'total_variants': total_candidates,
        'relation_groups_by_type': dict(relation_group_counts),
        'variation_points_by_type': {key: len(names)
                                     for key, names in variation_point_sets.items()},
        'variant_children_by_type': dict(variant_counts_by_type),
        'only_alternatives': only_alternatives,
        'materialize_single_arrays': materialize_single_arrays,
        'relation_types': dict(Counter(r['relation_type'] for r in rows)),
        'catalog_reasons': dict(status_counts),
        'alternative_catalog_reasons': dict(alternative_counts),
        'enumerated_alternative_points_in_profile': len(points) if only_alternatives else
            sum(1 for p in points if any(r['config_path'] == p['path'] and
                                         r['reason'] == 'enumerated_alternative'
                                         for r in rows)),
        'case_points': len(points), 'scalar_variations': sum(len(p['values']) for p in points),
        'boolean_feature_cases': len(feature_cases),
        'single_array_alternative_cases': len(array_cases),
        'sampled_paths': sampled_paths,
        'planned_total_cases': 1 + sum(len(p['values']) for p in points) + len(feature_cases) + len(array_cases),
        'coverage_note': 'Single-array object alternatives can be planned with explicit one-item arrays. '
                         'Nested arrays/maps remain catalogued; SAT and Kubernetes validation pending.',
    }
    return profile, rows, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root-feature', required=True,
                        help='Nombre EXACTO de la feature UVL que delimita el subárbol')
    parser.add_argument('--uvl', required=True, type=Path)
    parser.add_argument('--descriptions', required=True, type=Path)
    parser.add_argument('--metadata', required=True, type=Path)
    parser.add_argument('--mapper', type=Path, default=Path('metadata_parser01.py'))
    parser.add_argument('--rules', type=Path, help='sample_values y exclude_paths opcionales')
    parser.add_argument('--output-dir', type=Path, default=Path('experiments/uvl_variation'))
    parser.add_argument('--include-status', action='store_true',
                        help='Incluir decisiones de status (normalmente gestionado por Kubernetes)')
    parser.add_argument('--only-alternatives', action='store_true',
                        help='Generar casos solo para relaciones alternative; el CSV conserva el catálogo completo')
    parser.add_argument('--materialize-single-arrays', action='store_true',
                        help='Para alternative bajo un solo array de objetos: crear una instancia de prueba en el JSON')
    parser.add_argument('--max-cases', type=int, default=0,
                        help='Límite de casos incluido el base (0 significa sin límite)')
    args = parser.parse_args(argv)
    try:
        from flamapy.metamodels.fm_metamodel.operations import FMVariationPoints
        from flamapy.metamodels.fm_metamodel.transformations import UVLReader
        print(f'Leyendo UVL: {args.uvl}', flush=True)
        fm = UVLReader(str(args.uvl)).transform()
        selected_root = fm.get_feature_by_name(args.root_feature)
        if selected_root is None:
            raise ValueError(f'La feature {args.root_feature!r} no existe en el UVL')
        print(f'FMVariationPoints para el modelo; filtrando por {args.root_feature}...', flush=True)
        all_vps = FMVariationPoints().execute(fm).get_result()
        mapper_module = load_module('uvl_auto_metadata_mapper', args.mapper)
        mapper = mapper_module.MetadataMapper(str(args.descriptions), str(args.metadata))
        rules = json.loads(args.rules.read_text(encoding='utf-8')) if args.rules else {}
        profile, catalog, stats = make_uvl_profile(
            fm, mapper, selected_root, all_vps,
            rules=rules, include_status=args.include_status,
            max_cases=args.max_cases, only_alternatives=args.only_alternatives,
            materialize_single_arrays=args.materialize_single_arrays,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / 'auto_profile.json').write_text(
            json.dumps(profile, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        with (args.output_dir / 'variation_points.csv').open(
                'w', encoding='utf-8-sig', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=COLS)
            writer.writeheader()
            writer.writerows(catalog)
        (args.output_dir / 'discovery_summary.json').write_text(
            json.dumps(stats, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print('Recurso: {kind} ({api_version}) | VPs: {total_variation_points} | '
              'variantes: {total_variants} | casos OFAT: {planned_total_cases}'
              .format(**stats))
        print(f"Grupos de relaciones: {stats['relation_groups_by_type']}")
        print(f"VPs por tipo (un padre puede figurar en varios tipos): {stats['variation_points_by_type']}")
        print(f'Perfil: {args.output_dir / "auto_profile.json"}')
        print(f'Catálogo: {args.output_dir / "variation_points.csv"}')
        return 0
    except (OSError, ValueError, ImportError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
