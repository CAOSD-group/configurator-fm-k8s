#!/usr/bin/env python3
"""Auditoria independiente de defaults para RQ2 (no SAT, no Kubeconform).

Cuenta features marcadas como `default`, valores `defaultValue` en el FM
completo y coincidencias por instancia con el YAML reconstruido por el mapper.
La procedencia de un valor es INFERIDA, no un registro de eventos del generador.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


FIELDS = (
    'case_id', 'status', 'n_default_markers_selected',
    'n_default_markers_in_alternatives', 'n_defaultvalue_features_selected',
    'n_defaultvalue_full_equal', 'n_defaultvalue_yaml_equal_features',
    'n_defaultvalue_yaml_equal_occurrences', 'n_defaultvalue_yaml_different_features',
    'n_defaultvalue_inferred_applied', 'n_defaultvalue_explicit_equal',
    'n_defaultvalue_explicit_override', 'n_default_markers_mapped',
    'n_defaults_combined_full', 'n_defaults_combined_observed',
)


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def attr(feature, name):
    for a in feature.get_attributes():
        if a.name == name:
            return True, a.get_default_value()
    return False, None


def is_absence_default(feature):
    marked, absent = attr(feature, 'absenceDefault')
    _, source = attr(feature, 'defaultValueSource')
    _, value = attr(feature, 'defaultValue')
    return ((marked and absent is True) or
            (str(source).casefold() == 'description' and isinstance(value, str)
             and value.strip().casefold() in {'nil', 'null'}))


def load_index(uvl, root_name):
    from flamapy.metamodels.fm_metamodel.transformations import UVLReader
    model = UVLReader(str(uvl)).transform()
    root = model.get_feature_by_name(root_name)
    if root is None:
        raise ValueError(f'Feature root not found: {root_name}')
    markers, alternative_markers, scalar_defaults = set(), set(), {}
    stack = [root]
    while stack:
        feature = stack.pop()
        stack.extend(feature.get_children())
        if is_absence_default(feature):
            continue
        has_marker, _ = attr(feature, 'default')
        has_value, value = attr(feature, 'defaultValue')
        if has_marker:
            markers.add(feature.name)
        # value_for() uses typed defaults; the renderer may also materialize
        # scalar schema fields represented as Boolean *presence* features.
        _, schema_type = attr(feature, 'schema_type')
        if schema_type is None:
            _, schema_type = attr(feature, 'schemaType')
        typed_scalar = str(schema_type).lower() in {'string', 'integer', 'number'}
        if has_value and value is not None and (not feature.is_boolean() or typed_scalar):
            scalar_defaults[feature.name] = value
        if has_marker:
            parent = feature.get_parent()
            if parent and any(r.is_alternative() and feature in r.children
                              for r in parent.get_relations()):
                alternative_markers.add(feature.name)
    return markers, alternative_markers, scalar_defaults


def iter_scalars(obj, candidates, in_array=False):
    """Yield (feature, value, in_array), preserving repeated occurrences."""
    if isinstance(obj, list):
        for entry in obj:
            yield from iter_scalars(entry, candidates, True)
    elif isinstance(obj, dict):
        for name, value in obj.items():
            if name in candidates and not isinstance(value, (dict, list)):
                yield name, value, in_array
            yield from iter_scalars(value, candidates, in_array)


def resolve_path(row, key, folder, case):
    if folder is not None:
        return folder / f'{case}.json'
    raw = row.get(key)
    return Path(raw) if raw else None


def read_json(path):
    if path is None or not path.is_file():
        return None
    return json.loads(path.read_text(encoding='utf-8-sig'))


def same(a, b):
    return type(a) is type(b) and a == b


def audit(row, generation, markers, alternative_markers, defaults, args):
    case = row['case_id']
    result = dict.fromkeys(FIELDS, '')
    result.update(case_id=case, status='ok')
    full = read_json(resolve_path(row, 'full_config_json', args.full_config_dir, case))
    if not isinstance(full, dict):
        result['status'] = 'missing_or_invalid_full_config'
        return result

    chosen = {name for name, val in full.items() if val is not None and val is not False}
    selected_markers = markers & chosen
    selected_defaults = set(defaults) & chosen
    full_equal = {name for name in selected_defaults if
                  full[name] is not True and same(full[name], defaults[name])}
    result.update(n_default_markers_selected=len(selected_markers),
                  n_default_markers_in_alternatives=len(alternative_markers & chosen),
                  n_defaultvalue_features_selected=len(selected_defaults),
                  n_defaultvalue_full_equal=len(full_equal),
                  n_defaults_combined_full=len(selected_markers | full_equal))

    mapped = read_json(resolve_path(row, 'yaml_mapped_config_json', args.mapped_config_dir, case))
    equal_names = set()
    if isinstance(mapped, dict):
        equal_occurrences, different_names, mapped_markers = 0, set(), set()
        for name, val, _ in iter_scalars(mapped.get('config', mapped), selected_markers):
            if val is not None and val is not False:
                mapped_markers.add(name)
        for name, val, _ in iter_scalars(mapped.get('config', mapped), selected_defaults):
            if same(val, defaults[name]):
                equal_names.add(name)
                equal_occurrences += 1
            else:
                different_names.add(name)
        result.update(n_defaultvalue_yaml_equal_features=len(equal_names),
                      n_defaultvalue_yaml_equal_occurrences=equal_occurrences,
                      n_defaultvalue_yaml_different_features=len(different_names),
                      n_default_markers_mapped=len(mapped_markers),
                      n_defaults_combined_observed=len(mapped_markers | equal_names))
    else:
        result['status'] = 'missing_mapped_config'

    gen_row = generation.get(case, {})
    partial = read_json(resolve_path(gen_row, 'partial_json', args.partial_config_dir, case))
    if isinstance(partial, dict) and isinstance(mapped, dict):
        explicit, repeated = {}, set()
        for name, val, in_array in iter_scalars(partial.get('config', partial), selected_defaults):
            if in_array:
                repeated.add(name)  # No asignar origen a cada elemento sin identidad.
            elif val is not True and val is not None:
                explicit[name] = val
        inferred = equal_explicit = overridden = 0
        for name in selected_defaults - repeated:
            if name in explicit:
                if same(explicit[name], defaults[name]):
                    equal_explicit += 1
                else:
                    overridden += 1
            elif name in full_equal and name in equal_names:
                inferred += 1
        result.update(n_defaultvalue_inferred_applied=inferred,
                      n_defaultvalue_explicit_equal=equal_explicit,
                      n_defaultvalue_explicit_override=overridden)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--uvl', required=True, type=Path)
    p.add_argument('--root-feature', required=True)
    p.add_argument('--evaluation-csv', required=True, type=Path)
    p.add_argument('--generation-csv', type=Path)
    p.add_argument('--full-config-dir', type=Path)
    p.add_argument('--mapped-config-dir', type=Path)
    p.add_argument('--partial-config-dir', type=Path)
    p.add_argument('--output-dir', required=True, type=Path)
    args = p.parse_args(argv)

    markers, alternatives, defaults = load_index(args.uvl, args.root_feature)
    generation = ({r['case_id']: r for r in read_csv(args.generation_csv)}
                  if args.generation_csv else {})
    results = [audit(row, generation, markers, alternatives, defaults, args)
               for row in read_csv(args.evaluation_csv)]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / 'rq2_defaults.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(results)
    counters = [key for key in FIELDS if key.startswith('n_')]
    summary = {
        'cases': len(results), 'status_counts': dict(Counter(r['status'] for r in results)),
        'model_default_markers': len(markers),
        'model_alternative_default_markers': len(alternatives),
        'model_scalar_defaultvalue_features': len(defaults),
        'totals': {key: sum(r[key] for r in results if isinstance(r[key], int))
                   if any(isinstance(r[key], int) for r in results) else None
                   for key in counters},
        'note': ('A matching defaultValue is not necessarily an automatically applied default. '
                 'The inferred count requires missing explicit value, concrete full-config value '
                 'and presence in mapped YAML; repeated collection provenance is not asserted. '
                 'Counts are per feature except the occurrences column.'),
    }
    (args.output_dir / 'rq2_defaults_summary.json').write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'Processed {len(results)} cases -> {args.output_dir / "rq2_defaults.csv"}')


if __name__ == '__main__':
    main()