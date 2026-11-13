#!/usr/bin/env python3
"""Etapa 2 v5: evaluar YAML de CUALQUIER origen (FM, LLM, manual, etc.).

No llama a la generacion ni MaxSAT. SAT es una validacion opcional,
independiente de la generacion. Los campos que no pueden medirse
se dejan vacios: vacio != 0, y 'no evaluado' != 'valido'.

--mapper + --descriptions + --metadata aplican el MetadataMapper real para
construir JSON intermedios YAML->FM y diagnosticar elementos no mapeados.
--full-config-dir anade conteos exactos UVL cuando existen JSON completados
(estos ultimos NO son comparables directamente con casos solo-YAML).
"""
from __future__ import annotations

import argparse
import importlib.util
import csv
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml=None

AUDIT_COLS = [
    'rq1_point_feature','rq1_variant_full','rq1_variant_yaml',
    'rq1_vp_fidelity_status','rq1_instance_scope','rq1_audit_detail',
    'rq2_data_equivalent','rq2_documentation_status',
    'rq2_vp_block_status','rq2_selected_comment_status',
    'rq2_alternatives_expected','rq2_alternatives_verified',
    'rq2_comment_detail','rq2_selected_variant_has_default_metadata',
    'rq2_default_explicitly_labeled','time_audit_s',
]

COLS=[
    'case_id','source_id','manifest_yaml','full_config_json','status','error',
    'n_features','n_variation_points','n_variants','n_selected_variants',
    'n_defaults','pct_defaults',
    'n_defaults_observed_total',
    'n_default_value_features_selected','n_default_value_features_in_yaml',
    'n_default_value_features_matching','n_default_value_features_differing',
    'n_default_value_occurrences_matching','n_default_value_occurrences_differing',
    'n_default_value_features_unmapped',
    'n_default_value_inferred_applied','n_default_value_explicit_equal',
    'n_default_value_explicit_overrides',
    'default_value_audit_status','default_value_audit_detail',
    'n_alternative_groups_resolved','n_alternative_groups_unresolved',
    'yaml_syntax_valid','yaml_documents','yaml_kind','yaml_api_version',
    'yaml_expected_gvk_ok','yaml_distinct_paths','yaml_scalar_occurrences',
    'yaml_list_item_occurrences','yaml_object_occurrences',
    'yaml_metadata_matched_paths','yaml_metadata_unmatched_paths',
    'yaml_metadata_match_pct','yaml_matched_features','yaml_matched_alternative_variants',
    'yaml_matched_defaults','uvl_count_source','uvl_selected_features',
    'uvl_selected_alternative_groups','uvl_selected_alternative_variants',
    'uvl_selected_defaults','uvl_default_selection_pct',
    'uvl_alternative_defaults_active_pct',
    'uvl_available_alternative_groups','uvl_available_alternative_variants',
    'uvl_defaults_available','uvl_default_value_features_available',
    'schema_valid','schema_validation_status','schema_validation_detail',
    'fm_sat_valid','fm_sat_status','fm_sat_basis','fm_sat_detail',
    'fm_sat_boolean_presence_normalized',
    'semantic_valid','validation_status','validation_detail',
    'hallucinations','hallucinations_status',
    'time_read_yaml_s','time_yaml_parse_s','time_structure_s','time_model_metrics_s',
    'time_validation_s','time_kubeconform_s','time_fm_sat_s',
    'time_evaluation_total_s',
    'yaml_mapping_status','yaml_mapping_detail','yaml_mapped_config_json',
    'yaml_mapper_unmapped','yaml_mapper_ignored',
    'time_yaml_to_fm_s',
    'rq3_target_point_feature','rq3_target_config_path',
    'rq3_target_path_present','rq3_target_path_occurrences',
    'rq3_point_feature_mapped',
    'rq3_variant_matches','rq3_variant_match_count',
    'rq3_requirement_status','rq3_requirement_detail',
] + AUDIT_COLS


# Resumen de métricas de investigación. El CSV completo conserva diagnósticos.
CORE_COLS = [
    'case_id','source_id','manifest_yaml','status',
    'n_features','n_variation_points','n_variants','n_selected_variants',
    'n_defaults','pct_defaults',
    'n_defaults_observed_total',
    'n_default_value_features_selected','n_default_value_features_in_yaml',
    'n_default_value_features_matching','n_default_value_features_differing',
    'n_default_value_occurrences_matching','n_default_value_occurrences_differing',
    'n_default_value_features_unmapped',
    'n_default_value_inferred_applied','n_default_value_explicit_equal',
    'n_default_value_explicit_overrides',
    'default_value_audit_status',
    'yaml_matched_features','yaml_matched_alternative_variants','yaml_matched_defaults',
    'yaml_mapping_status',
    'yaml_syntax_valid','schema_valid','schema_validation_status',
    'fm_sat_valid','fm_sat_status','fm_sat_basis',
    'semantic_valid','validation_status',
    'hallucinations','hallucinations_status',
    'rq3_target_path_present','rq3_point_feature_mapped',
    'rq3_variant_match_count','rq3_requirement_status',
    'rq1_vp_fidelity_status','rq2_data_equivalent','rq2_documentation_status',
]


def save_csv(path, rows, columns=COLS):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=columns,extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def append_csv_row(path, row, columns=COLS):
    """Append a single row without rewriting all previous cases (O(n) per run)."""
    with path.open('a',encoding='utf-8-sig',newline='') as fh:
        writer=csv.DictWriter(fh,fieldnames=columns,extrasaction='ignore')
        writer.writerow(row)


def selected(v):
    return v is not None and v is not False


def belongs(feature,root):
    while feature is not None:
        if feature is root:
            return True
        feature=feature.get_parent()
    return False


def index_uvl(path,root_feature):
    from flamapy.metamodels.fm_metamodel.transformations import UVLReader
    from flamapy.metamodels.fm_metamodel.operations import FMVariationPoints
    fm=UVLReader(str(path)).transform()
    root=fm.get_feature_by_name(root_feature)
    if root is None:
        raise ValueError(f'UVL root feature not found: {root_feature}')
    features={f.name:f for f in fm.get_features()}
    names={f.name for f in fm.get_features() if belongs(f,root)}
    groups=[]
    for vp in FMVariationPoints().execute(fm).get_result():
        if belongs(vp,root):
            for rel in vp.get_relations():
                if rel.is_alternative():
                    groups.append((vp,rel))

    def is_default(feature):
        for a in feature.get_attributes():
            if a.name=='default':
                return True
        return False

    defaults={name for name in names if is_default(features[name])}
    # `default` denotes a preferred FEATURE in a group. `defaultValue`
    # denotes a reference concrete VALUE; they must not be conflated.
    default_values={}
    for name in names:
        for attribute in features[name].get_attributes():
            if attribute.name == 'defaultValue':
                value = attribute.get_default_value()
                if value is not None:
                    default_values[name] = value
                break
    alt_children={c.name for vp,rel in groups for c in rel.children}
    return {'fm':fm,'features':features,'names':names,'groups':groups,'defaults':defaults,
            'default_values':default_values,
            'alt_children':alt_children,
            'available_alternative_groups':len(groups),
            'available_alternative_variants':sum(len(rel.children) for _,rel in groups),
            'defaults_available':len(defaults),
            'default_value_features_available':len(default_values)}


def read_metadata(path):
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data,dict) or not isinstance(data.get('features'),list):
        raise ValueError('Expected serialization_metadata JSON with a "features" array')
    index={}
    for entry in data['features']:
        if not isinstance(entry,dict):
            continue
        p=entry.get('configPath')
        if isinstance(p,str) and p:
            index.setdefault(p,[]).append(entry)
    return index


def walk_yaml(doc):
    """Visit YAML schema-like paths; array items are represented as [].
    Map keys are not guessed to be schema properties.
    """
    occurrences=[]
    stat=Counter()
    visited=set()

    def walk(value,path):
        if isinstance(value,(list,dict)):
            ident=id(value)
            if ident in visited:
                return
            visited.add(ident)
        if path:
            occurrences.append((path,value))
        if isinstance(value,dict):
            stat['objects']+=1
            for key,child in value.items():
                if isinstance(key,str):
                    walk(child,f'{path}.{key}' if path else key)
        elif isinstance(value,list):
            stat['list_items']+=len(value)
            for item in value:
                walk(item,path+'[]')
        else:
            stat['scalars']+=1

    walk(doc,'')
    return occurrences,stat


def same_value(a,b):
    # Bool and int compare equal in Python; match exact type for robust enums.
    return type(a) is type(b) and a==b


def match_metadata(occurrences,index,allowed=None):
    """Conservative YAML->UVL projection: present paths + exact enum matches.

    Cannot establish a complete SAT configuration or prove selections of
    absent, abstract, conditional, or dynamic-map features.
    """
    paths={path for path,_ in occurrences if path}
    matched_paths=set()
    selected_names=set()
    for path,value in occurrences:
        entries=index.get(path,())
        if not entries:
            continue
        valid_entries=[e for e in entries if isinstance(e.get('feature'),str)
                       and (allowed is None or e['feature'] in allowed)]
        if not valid_entries:
            continue
        matched_paths.add(path)
        for e in valid_entries:
            name=e['feature']
            if e.get('serializationRole')=='value' and e.get('synthetic') is True:
                if 'configValue' in e and same_value(value,e['configValue']):
                    selected_names.add(name)
            elif e.get('serializationRole') in ('property','structural') and e.get('synthetic') is not True:
                # Represents a property PRESENT in the YAML, not an FM SAT selection.
                selected_names.add(name)
    return selected_names,matched_paths,paths-matched_paths


def load_metadata_mapper(module_path, descriptions_path, serialization_metadata_path, strict=False):
    """Load the real metadata_parser01.py from the supplied absolute/relative path."""
    path = Path(module_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'Metadata mapper not found: {path}')
    spec = importlib.util.spec_from_file_location('yaml_to_uvl_metadata_mapper', str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f'Cannot import MetadataMapper from {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, 'MetadataMapper'):
        raise ImportError(f'No MetadataMapper in {path}')
    return module.MetadataMapper(
        str(descriptions_path), str(serialization_metadata_path), strict=strict
    )


def mapped_config_decisions(mapped_config, root_name=None):
    """Extract positive/negative SAT hypotheses from *one* mapper-produced document.

    Nested ordinary object features are expanded. Collection members are read
    for coverage and SAT projection, but instance identities are not expressible
    in the Boolean FM. Do not mistake SAT extension for YAML semantic validity.
    """
    if not isinstance(mapped_config, dict) or not isinstance(mapped_config.get('config'), dict):
        raise ValueError('Expected MetadataMapper output of form {"config": {...}}')
    hypotheses = {}
    conflicting_values = set()
    instances = 0

    def put(feature, value):
        if feature in hypotheses and hypotheses[feature] != value:
            conflicting_values.add(feature)
        else:
            hypotheses[feature] = value

    def walk(mapping):
        nonlocal instances
        for feature, value in mapping.items():
            if not isinstance(feature, str):
                raise ValueError('MetadataMapper produced a non-string feature identifier')
            if isinstance(value, dict):
                put(feature, True)
                walk(value)
            elif isinstance(value, list):
                put(feature, True)
                instances += len(value)
                for item in value:
                    if isinstance(item, dict):
                        walk(item)
                    else:
                        raise ValueError(f'Unexpected collection item for {feature}')
            else:
                put(feature, value)

    walk(mapped_config['config'])
    if root_name:
        put(root_name, True)  # GVK identifies this manifest root unambiguously
    return hypotheses, conflicting_values, instances


def is_schema_boolean_value_feature(model, name):
    """Identify a YAML Boolean *value field*, not a Boolean SAT decision.

    In the UVL, e.g. `ready {schema_type 'boolean'}`, the feature variable
    represents the PRESENCE of the property. A YAML `ready: false` is therefore
    a present feature with the concrete value False, NOT an absent feature.
    """
    feature = (model or {}).get('features', {}).get(name)
    if feature is None:
        return False
    for attr in feature.get_attributes():
        if attr.name in ('schema_type', 'schemaType'):
            try:
                field_type = attr.get_default_value()
            except (AttributeError, TypeError, ValueError):
                continue
            return str(field_type).strip().lower() in ('boolean', 'bool')
    return False


def project_yaml_hypotheses_for_boolean_sat(hypotheses, model):
    """Project mapped YAML values into Boolean feature-presence assumptions.

    Preserve the original MetadataMapper JSON (including actual False values).
    Only for SAT, translate False -> selected feature for schema-typed Boolean
    properties. A negative Boolean SAT assumption can only be justified by
    explicit *absence of a feature*, not by its YAML value being false.
    Unrecognized/untagged False decisions are not silently changed.
    """
    projected = dict(hypotheses)
    normalized = []
    for feature, value in hypotheses.items():
        if value is False and is_schema_boolean_value_feature(model, feature):
            projected[feature] = True
            normalized.append(feature)
    return projected, normalized


def ambiguous_collection_alternatives(hypotheses, model):
    """Multiple chosen children from one alternative group cannot be verified
    accurately without per-instance variables. Classify them as 'unavailable'."""
    if model is None:
        return []
    selected_features = {k for k, v in hypotheses.items() if selected(v)}
    return [vp.name for vp, relation in model['groups']
            if sum(child.name in selected_features for child in relation.children) > 1]


def convert_yaml_with_mapper(mapper, docs, output_dir, case_id, model=None):
    """Return mapped documents, their positive feature projection and diagnostics.

    Persist the authoritative intermediate representation, not a hand-built path
    heuristic. MetadataMapper resets its report per document, so aggregate it.
    """
    mapped_documents = []
    hypotheses_list = []
    reports = []
    duplicate_features = set()
    collection_items = 0
    for document in docs:
        mapped = mapper.transform_manifest(document)
        report = mapper.mapping_report()
        root_name = report.get('rootFeature')
        decisions, conflicting, instances = mapped_config_decisions(mapped, root_name)
        mapped_documents.append(mapped)
        hypotheses_list.append(decisions)
        reports.append(report)
        duplicate_features.update(conflicting)
        collection_items += instances
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f'{case_id}.json'
    payload = mapped_documents[0] if len(mapped_documents) == 1 else {'documents': mapped_documents}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    unmapped = [issue for rep in reports for issue in rep.get('unmapped', [])]
    ignored = [issue for rep in reports for issue in rep.get('ignored', [])]
    # Coverage counts are based on feature PRESENCE. Boolean-valued fields
    # `false` are still present, even though their concrete YAML value is false.
    projected_list = [project_yaml_hypotheses_for_boolean_sat(decisions, model)[0]
                      for decisions in hypotheses_list]
    selected_features = {name for decisions in projected_list for name, val in decisions.items()
                         if selected(val)}
    return {
        'configs': mapped_documents,
        'hypotheses': hypotheses_list,
        'selected_features': selected_features,
        'unmapped': unmapped,
        'ignored': ignored,
        'conflicting_values': duplicate_features,
        'collection_items': collection_items,
        'mapped_path': out,
    }


def _all_mapped_scalar_values(mapped_result, default_value_features):
    """Mapped scalar occurrences keyed by UVL feature name.

    Each collection item contributes its own occurrence; an aggregate list
    must NOT be treated as one value. None is retained as an actual YAML null.
    """
    out = {}
    if mapped_result is None:
        return out

    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            for name, value in obj.items():
                if name in default_value_features and not isinstance(value, (list, dict)):
                    out.setdefault(name, []).append(value)
                if isinstance(value, (list, dict)):
                    walk(value)

    for config in mapped_result.get('configs', []):
        if isinstance(config, dict):
            walk(config.get('config', {}))
    return out


def _partial_explicit_values(path, allowed_names):
    """Read optional original partial FM configuration; never infer absent input.

    Supports {feature:value} and {"config": {feature:value}}, recursively
    handling nested feature dictionaries. Repeated collection values are
    marked ambiguous rather than assuming a single instance.
    """
    if path is None or not path.is_file():
        return None
    obj = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(obj, dict):
        raise ValueError('Partial configuration must be a JSON object')
    out = {}

    def walk(mapping):
        if not isinstance(mapping, dict):
            return
        for name, value in mapping.items():
            if name in allowed_names and not isinstance(value, (dict, list)):
                out[name] = value
            elif isinstance(value, dict):
                walk(value)
            elif isinstance(value, list):
                # Per-item provenance is not provable with Boolean FM variables.
                for item in value:
                    if isinstance(item, dict):
                        walk(item)

    walk(obj)
    return out


def audit_default_values(row, model, mapped_result, full_path, partial_path=None):
    """Count model defaults vs. YAML defaults, without claiming value provenance.

    - `default`: a selected preferred alternative (existing n_defaults).
    - `defaultValue`: a selected property whose concrete YAML value is equal
      to the CAFM reference value (a match, not necessarily a generated default).
    - Provenance from an *optional original partial configuration* permits
      only an INFERENCE of fallback, never an assertion of engine instrumentation.

    Returns detail rows for traceability and the current case's CSV metrics.
    """
    if model is None:
        return [], {'default_value_audit_status': 'unavailable_no_uvl'}
    declared = model.get('default_values', {})
    values = _all_mapped_scalar_values(mapped_result, declared)
    if mapped_result is None:
        return [], {'default_value_audit_status': 'unavailable_no_mapper'}

    selected_full = None
    if full_path is not None and full_path.is_file():
        full = json.loads(full_path.read_text(encoding='utf-8-sig'))
        if not isinstance(full, dict):
            raise ValueError('Completed configuration JSON must be an object')
        # Schema boolean `false` means a present value, not necessarily an
        # absent feature; check mapped presence to disambiguate it.
        selected_full = {name for name, value in full.items() if name in model['names']
                         and (selected(value) or
                              (value is False and name in values and
                               is_schema_boolean_value_feature(model, name)))}
    partial = _partial_explicit_values(partial_path, model['names'])

    observed_feature_names = set(values)
    candidate_names = (set(declared) & selected_full) if selected_full is not None else observed_feature_names
    details = []
    match_features, different_features, unmapped_features = set(), set(), set()
    match_occurrences = different_occurrences = 0
    inferred = explicit_equal = explicit_override = 0

    # A property may have multiple actual occurrences in collection instances.
    # Maintain both distinct-feature and per-occurrence metrics.
    # With a full FM configuration, restrict comparisons to selected
    # features; a mapped feature not selected in the FM is a separate
    # consistency issue, not evidence of a used default.
    audit_names = candidate_names if selected_full is not None else observed_feature_names
    for name in sorted(audit_names):
        if name not in declared:
            continue
        default = declared[name]
        observed = values.get(name, [])
        if not observed:
            unmapped_features.add(name)
            details.append({'feature': name, 'default_value': json.dumps(default, ensure_ascii=False, default=str),
                            'yaml_value': '', 'match': '', 'source': 'not_in_mapped_yaml',
                            'occurrence': ''})
            continue
        for idx, concrete in enumerate(observed, 1):
            match = same_value(default, concrete)
            if match:
                match_features.add(name)
                match_occurrences += 1
            else:
                different_features.add(name)
                different_occurrences += 1
            source = 'matching_value_provenance_unknown' if match else 'non_default_value'
            if partial is not None:
                initial = partial.get(name, None)
                # True is an unspecified typed-feature placeholder in this
                # generator's FM configuration representation; False is kept.
                explicitly_valued = name in partial and initial is not True and initial is not None
                if explicitly_valued:
                    if match and same_value(initial, default):
                        source = 'explicit_equal_to_default'
                        explicit_equal += 1
                    elif not match and same_value(initial, concrete):
                        source = 'explicit_override_of_default'
                        explicit_override += 1
                    else:
                        source = 'explicit_value_disagrees_or_ambiguous'
                elif match:
                    source = 'inferred_default_fallback_not_proven'
                    inferred += 1
            details.append({'feature': name,
                            'default_value': json.dumps(default, ensure_ascii=False, default=str),
                            'yaml_value': json.dumps(concrete, ensure_ascii=False, default=str),
                            'match': match, 'source': source, 'occurrence': idx})

    # Compute the union on FEATURE names (no accidental double count when one
    # feature has both a `default` marker and a `defaultValue` attribute).
    selected_defaults = (model['defaults'] & selected_full) if selected_full is not None else set()
    total = len(selected_defaults | match_features) if selected_full is not None else ''
    result = {
        'n_defaults_observed_total': total,
        'n_default_value_features_selected': len(candidate_names) if selected_full is not None else '',
        'n_default_value_features_in_yaml': len(observed_feature_names),
        'n_default_value_features_matching': len(match_features),
        'n_default_value_features_differing': len(different_features),
        'n_default_value_occurrences_matching': match_occurrences,
        'n_default_value_occurrences_differing': different_occurrences,
        'n_default_value_features_unmapped': len(unmapped_features) if selected_full is not None else '',
        'n_default_value_inferred_applied': inferred if partial is not None else '',
        'n_default_value_explicit_equal': explicit_equal if partial is not None else '',
        'n_default_value_explicit_overrides': explicit_override if partial is not None else '',
        'default_value_audit_status': (
            'checked_with_partial_inference' if partial is not None else
            'checked_partial_file_missing' if partial_path is not None else
            'checked_no_provenance'),
        'default_value_audit_detail': (
            'defaultValue matching is value equality, NOT proof that the generator applied a fallback. '
            'Optional partial-config classification is inferred, not generator-traced; '
            'collection occurrences may share one FM feature. '
            'n_defaults_observed_total is a distinct-feature union of selected default markers '
            'and YAML properties matching defaultValue.'),
    }
    return details, result


def compute_uvl_metrics(row,model,full_path,matched):
    if model is None:
        return
    row.update(uvl_available_alternative_groups=model['available_alternative_groups'],
               uvl_available_alternative_variants=model['available_alternative_variants'],
               uvl_defaults_available=model['defaults_available'])
    row['yaml_matched_alternative_variants']=len(matched & model['alt_children']) if matched is not None else ''
    row['yaml_matched_defaults']=len(matched & model['defaults']) if matched is not None else ''
    if not full_path:
        return
    if not full_path.is_file():
        row['uvl_count_source'] = 'full_configuration_json_missing'
        return
    data=json.loads(full_path.read_text(encoding='utf-8-sig'))
    if not isinstance(data,dict):
        raise ValueError('Completed configuration JSON must be an object')
    names=model['names']
    sf={k for k,v in data.items() if k in names and selected(v)}
    groups=[(p,rel) for p,rel in model['groups'] if p.name in sf]
    groups_with_default=[(p,rel) for p,rel in groups if any(c.name in model['defaults'] for c in rel.children)]
    used=sum(any(c.name in sf and c.name in model['defaults'] for c in rel.children)
             for _,rel in groups_with_default)
    defaults_used=len(sf & model['defaults'])
    row.update(
        uvl_count_source='full_configuration_json',
        uvl_selected_features=len(sf),
        uvl_selected_alternative_groups=len(groups),
        uvl_selected_alternative_variants=sum(sum(c.name in sf for c in rel.children) for _,rel in groups),
        uvl_selected_defaults=defaults_used,
        uvl_default_selection_pct=round(100*defaults_used/len(model['defaults']),3) if model['defaults'] else '',
        uvl_alternative_defaults_active_pct=round(100*used/len(groups_with_default),3) if groups_with_default else '',
    )
    selected_by_group = [sum(c.name in sf for c in rel.children) for _, rel in groups]
    row.update(
        n_features=len(sf),
        n_variation_points=len(groups),
        n_variants=sum(len(rel.children) for _,rel in groups),
        n_selected_variants=sum(selected_by_group),
        n_defaults=defaults_used,
        pct_defaults=round(100*defaults_used/len(model['defaults']),3)
                     if model['defaults'] else '',
        n_alternative_groups_resolved=sum(n==1 for n in selected_by_group),
        n_alternative_groups_unresolved=sum(n==0 for n in selected_by_group),
    )
    row['full_config_json']=str(full_path.resolve())



class FMSATValidator:
    """Check whether boolean feature assumptions have SOME SAT extension.

    Does NOT validate exact feature-set identity, concrete collection instances,
    numeric/string feature attributes, or Kubernetes semantics. Build once per run.
    """

    def __init__(self, fm):
        from flamapy.metamodels.pysat_metamodel.transformations import FmToPysat
        from pysat.solvers import Solver
        sat_model = FmToPysat(fm).transform()
        self.variables = sat_model.variables
        self.solver = Solver(name='glucose3')
        for clause in sat_model.get_all_clauses():
            self.solver.add_clause(clause)

    def check(self, features):
        unknown = sorted(set(features) - set(self.variables))
        if unknown:
            return None, f'{len(unknown)} unknown feature(s): {", ".join(unknown[:5])}'
        assumptions = [self.variables[k] if selected(v) else -self.variables[k]
                       for k, v in features.items()]
        return bool(self.solver.solve(assumptions=assumptions)), ''

    def close(self):
        self.solver.delete()


def fm_sat_check(mode, sat_validator, model, full_path, yaml_matched, mapped_result=None):
    if mode == 'none':
        return {'fm_sat_status': 'not_run'}
    if sat_validator is None:
        return {'fm_sat_status':'unavailable','fm_sat_detail':'No SAT model'}
    if mode in ('full', 'auto') and full_path is not None and full_path.is_file():
        try:
            full = json.loads(full_path.read_text(encoding='utf-8-sig'))
            if not isinstance(full,dict):
                raise ValueError('Full configuration must be an object')
            # Collections are serialized separately: SAT only sees top-level
            # values in the current generator. Do not pretend to validate items.
            decisions = {k:v for k,v in full.items()
                         if k in model.get('features', model['names'])
                         and not isinstance(v,(dict,list))}
            checked, detail = sat_validator.check(decisions)
        except (OSError, ValueError) as e:
            return {'fm_sat_status':'error','fm_sat_detail':str(e)[:1500]}
        return {'fm_sat_status': 'passed' if checked else 'rejected' if checked is False else 'unavailable',
                'fm_sat_valid': checked if checked is not None else '',
                'fm_sat_basis':'full_config_assumptions', 'fm_sat_detail':detail}
    if mode in ('yaml', 'auto'):
        if mapped_result is None:
            return {'fm_sat_status':'unavailable',
                    'fm_sat_detail':'No MetadataMapper result; use --mapper, --descriptions and --metadata'}
        hypotheses_list = mapped_result['hypotheses']
        if len(hypotheses_list) != 1:
            return {'fm_sat_status':'unavailable',
                    'fm_sat_detail':'SAT of multi-document manifest must be checked per resource'}
        hypotheses = hypotheses_list[0]
        if not hypotheses:
            return {'fm_sat_status':'unavailable', 'fm_sat_detail':'Mapper produced no SAT hypotheses'}
        if mapped_result['conflicting_values']:
            return {'fm_sat_status':'unavailable',
                    'fm_sat_detail':'Conflicting repeated feature values: '+
                                    ', '.join(sorted(mapped_result['conflicting_values'])[:5])}
        conflicts = ambiguous_collection_alternatives(hypotheses, model)
        if conflicts:
            return {'fm_sat_status':'unavailable',
                    'fm_sat_detail':'Alternative group has >1 selected variant (may belong to different collection instances): '+
                                    ', '.join(conflicts[:5])}
        sat_hypotheses, normalized = project_yaml_hypotheses_for_boolean_sat(hypotheses, model)
        checked, detail = sat_validator.check(sat_hypotheses)
        note = ('Boolean YAML values projected to present features: ' +
                ', '.join(normalized[:8]) + (' ...' if len(normalized) > 8 else '') + '. ')
        if not normalized:
            note = ''
        return {'fm_sat_status': 'passed' if checked else 'rejected' if checked is False else 'unavailable',
                'fm_sat_valid': checked if checked is not None else '',
                'fm_sat_basis':'metadata_mapper_partial_config',
                'fm_sat_boolean_presence_normalized':len(normalized),
                'fm_sat_detail': ('SAT checks an extension of mapped hypotheses, not YAML semantic validity. ' + note + detail).strip()}
    return {'fm_sat_status':'not_run','fm_sat_detail':'No usable full config or mapped YAML configuration'}


def kubeconform_check(path, version, timeout, schema_location=None, cache_dir=None):
    """Kubernetes JSON Schema validation; not semantic/admission validation.

    Unknown schema, missing executable or network errors remain UNKNOWN, never False.
    """
    executable = shutil.which('kubeconform')
    if not executable:
        return {'schema_validation_status':'unavailable',
                'schema_validation_detail':'kubeconform not found in PATH'}
    cmd = [executable, '-strict', '-summary', '-output', 'json',
           '-kubernetes-version', version]
    if schema_location:
        cmd += ['-schema-location', schema_location]
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cmd += ['-cache', str(cache_dir.resolve())]
    cmd.append(str(path))
    try:
        result = subprocess.run(cmd,capture_output=True,text=True,timeout=timeout,check=False)
    except (OSError,subprocess.TimeoutExpired) as e:
        return {'schema_validation_status':'unavailable',
                'schema_validation_detail':str(e)[:2000]}
    try:
        data = json.loads(result.stdout)
        resources = data.get('resources',[])
        if not isinstance(resources,list):
            raise ValueError('Expected resources list')
        summary = data.get('summary',{})
        if not isinstance(summary,dict):
            raise ValueError('Expected summary')
        invalid = int(summary.get('invalid',0))
        errors = int(summary.get('errors',0))
        skipped = int(summary.get('skipped',0))
        valid = int(summary.get('valid',0))
        messages = ' | '.join(str(r.get('msg','')) for r in resources if r.get('msg'))[:2400]
        if invalid>0:
            return {'schema_valid':False,'schema_validation_status':'rejected',
                    'schema_validation_detail':messages}
        if errors>0 or skipped>0 or valid==0 or result.returncode != 0:
            return {'schema_validation_status':'unavailable',
                    'schema_validation_detail': f'valid={valid}, errors={errors}, skipped={skipped}; {messages}'}
        return {'schema_valid':True,'schema_validation_status':'passed',
                'schema_validation_detail': f'{valid} resource(s) validated'}
    except (ValueError, TypeError, AttributeError) as e:
        return {'schema_validation_status':'unavailable',
                'schema_validation_detail': f'{e}: '+(result.stdout or result.stderr)[-1800:]}


def kubectl_check(path,mode,timeout):
    if mode=='none':
        return {'validation_status':'not_run'}
    exe=shutil.which('kubectl')
    if not exe:
        return {'validation_status':'unavailable','validation_detail':'kubectl not found'}
    command=[exe,'apply',f'--dry-run={mode}','-f',str(path),'--validate=strict']
    try:
        p=subprocess.run(command,capture_output=True,text=True,timeout=timeout,check=False)
    except (subprocess.TimeoutExpired,OSError) as e:
        return {'validation_status':'unavailable','validation_detail':str(e)[:1500]}
    output=((p.stderr or '')+'\n'+(p.stdout or '')).strip()[-2500:]
    if p.returncode==0:
        return {'validation_status':'passed','validation_detail':output,
                'semantic_valid':True if mode=='server' else ''}
    low=output.lower()
    infra=('unable to connect','connection refused','no configuration has been provided',
           'no such host','dial tcp','i/o timeout','context deadline exceeded',
           'connection reset','the server has asked for the client to provide credentials',
           'unauthorized','forbidden','failed to download openapi',
           'couldn\'t get current server api group list','unable to retrieve the complete list',
           'tls handshake timeout','x509:')
    if any(text in low for text in infra):
        return {'validation_status':'unavailable','validation_detail':output}
    schema=('strict decoding error','error validating data:','validationerror(',
            'unknown field','missing required field','failed to create typed patch object')
    # May reflect server validation, not necessarily only OpenAPI schema.
    if any(x in low for x in schema):
        return {'validation_status':'rejected','validation_detail':output,
                'semantic_valid':False if mode=='server' else ''}
    if mode=='server' and re.search(r'error from server \((Invalid|BadRequest)\)',output,re.I):
        return {'validation_status':'rejected','validation_detail':output,'semantic_valid':False}
    return {'validation_status':'error','validation_detail':output}


# ---------------------------------------------------------------
# RQ3/common requested variation-point check.
# ---------------------------------------------------------------

def load_cases_json(path):
    """Load benchmark/prompt cases indexed by case_id."""
    if path is None:
        return {}
    payload=json.loads(path.read_text(encoding='utf-8-sig'))
    cases=payload.get('cases') if isinstance(payload,dict) else payload if isinstance(payload,list) else None
    if not isinstance(cases,list):
        raise ValueError('--cases-json must contain a "cases" array or be a JSON array')
    indexed={}
    duplicates=[]
    for case in cases:
        if not isinstance(case,dict):
            continue
        case_id=case.get('case_id')
        if not isinstance(case_id,str) or not case_id:
            continue
        if case_id in indexed:
            duplicates.append(case_id)
        indexed[case_id]=case
    if duplicates:
        raise ValueError('Duplicate case_id in --cases-json: '+', '.join(sorted(set(duplicates))[:10]))
    return indexed


def rq3_target_check(case_meta, occurrences, mapped_result=None, mapping_status='not_run'):
    """Check whether the YAML->FM mapping contains the requested point_feature.

    point_feature membership in MetadataMapper selected_features is the primary
    RQ3 satisfaction signal. config_path presence is auxiliary YAML evidence.
    """
    out={
        'rq3_target_point_feature':'',
        'rq3_target_config_path':'',
        'rq3_target_path_present':'',
        'rq3_target_path_occurrences':'',
        'rq3_point_feature_mapped':'',
        'rq3_variant_matches':'',
        'rq3_variant_match_count':'',
        'rq3_requirement_status':'not_run',
        'rq3_requirement_detail':'',
    }
    if case_meta is None:
        out['rq3_requirement_status']='case_not_found'
        return out
    point=case_meta.get('point_feature') or ''
    config_path=case_meta.get('config_path') or ''
    variants=case_meta.get('variant_features') or []
    if not isinstance(variants,list):
        variants=[]
    out['rq3_target_point_feature']=point
    out['rq3_target_config_path']=config_path
    if not point:
        out['rq3_requirement_status']='case_missing_point_feature'
        return out
    if config_path:
        count=sum(1 for path,_ in occurrences if path==config_path)
        out['rq3_target_path_occurrences']=count
        out['rq3_target_path_present']=count>0
    if mapped_result is None:
        out['rq3_requirement_status']='mapping_error' if mapping_status=='mapping_error' else 'mapping_unavailable'
        return out
    selected_features=set(mapped_result.get('selected_features') or [])
    point_mapped=point in selected_features
    out['rq3_point_feature_mapped']=point_mapped
    matches=sorted(set(variants) & selected_features)
    out['rq3_variant_matches']=';'.join(matches)
    out['rq3_variant_match_count']=len(matches)
    if not point_mapped:
        if out['rq3_target_path_present'] is True:
            out['rq3_requirement_status']='path_present_point_unmapped'
            out['rq3_requirement_detail']='config_path is present, but MetadataMapper did not reconstruct point_feature; review mapping diagnostics.'
        else:
            out['rq3_requirement_status']='target_point_missing'
        return out
    if not variants:
        out['rq3_requirement_status']='satisfied'
    elif len(matches)==1:
        out['rq3_requirement_status']='satisfied_variant_resolved'
    elif len(matches)==0:
        out['rq3_requirement_status']='satisfied_variant_unresolved'
        out['rq3_requirement_detail']='point_feature was reconstructed, but no expected variant_feature was identified.'
    else:
        out['rq3_requirement_status']='satisfied_multiple_variants_instance_ambiguous'
        out['rq3_requirement_detail']='Multiple expected variants were reconstructed; repeated collection instances cannot be separated by the Boolean FM.'
    return out


# ---------------------------------------------------------------
# RQ1/RQ2: optional per-case audits; these do not rerun SAT/MaxSAT.
# ---------------------------------------------------------------

def load_case_table(path):
    if path is None:
        return {}
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        return {r['case_id']: r for r in csv.DictReader(handle) if r.get('case_id')}


def attribute_value(feature, key):
    for attr in feature.get_attributes():
        if attr.name == key:
            return attr.get_default_value()
    return None


def normalized_doc(feature):
    doc = attribute_value(feature, 'doc')
    return ' '.join(str(doc).split()) if doc is not None else None


def format_comment_value(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def comment_variant_description(feature):
    key = attribute_value(feature, 'configKey')
    value = attribute_value(feature, 'configValue')
    if key is None:
        label = format_comment_value(value) if value is not None else feature.name
    elif value is not None:
        label = f'{key}: {format_comment_value(value)}'
    else:
        label = str(key)
    doc = normalized_doc(feature)
    return f'{label} ({doc})' if doc else label


def strict_tree(value):
    # Preserve scalar type (bool != integer), ignore mapping insertion order.
    if isinstance(value, dict):
        return ('map', tuple(sorted((strict_tree(k),strict_tree(v)) for k,v in value.items())))
    if isinstance(value, list):
        return ('list', tuple(strict_tree(item) for item in value))
    return (type(value).__qualname__, repr(value))


def comment_blocks(raw):
    """Extract only explicit VP blocks, not unrelated inline YAML comments."""
    blocks=[]
    active=None
    for line in raw.splitlines():
        match = re.match(r'^\s*#\s*(.*?)\s*$', line)
        if not match:
            active=None
            continue
        item=match.group(1)
        if item.startswith('[Variability Point: '):
            active=[item]
            blocks.append(active)
        elif item.startswith('- ') and active is not None:
            active.append(item)
        else:
            active=None
    return blocks


def get_point_relation(model, point):
    feat = model['features'].get(point) if model else None
    if feat is None:
        return None, None
    relations=[rel for rel in feat.get_relations() if rel.is_alternative()]
    if len(relations)!=1:
        return feat, None
    return feat, relations[0]


def audit_rq1(plan_row, gen_row, model, full_path, mapped_result):
    out={k:'' for k in AUDIT_COLS}
    point=(plan_row or {}).get('point_feature') or (plan_row or {}).get('choice_feature') or ''
    out['rq1_point_feature']=point
    if not point:
        out['rq1_vp_fidelity_status']='base_or_no_point'
        return out
    feat,rel=get_point_relation(model, point)
    if rel is None:
        out['rq1_vp_fidelity_status']='unknown_or_nonunique_vp'
        return out
    if full_path is None or not full_path.is_file():
        out['rq1_vp_fidelity_status']='missing_full_config'
        return out
    if mapped_result is None or len(mapped_result.get('configs',[]))!=1:
        out['rq1_vp_fidelity_status']='mapping_unavailable'
        return out
    try:
        full=json.loads(full_path.read_text(encoding='utf-8-sig'))
    except (ValueError,OSError) as exc:
        out['rq1_vp_fidelity_status']='full_read_error'
        out['rq1_audit_detail']=str(exc)[:400]
        return out
    if not isinstance(full,dict):
        out['rq1_vp_fidelity_status']='invalid_full_config'
        return out
    chosen_full=[c.name for c in rel.children if selected(full.get(c.name))]
    chosen_yaml=[c.name for c in rel.children if c.name in mapped_result['selected_features']]
    out['rq1_variant_full']=';'.join(chosen_full)
    out['rq1_variant_yaml']=';'.join(chosen_yaml)
    def has_multiple_arrays(node):
        if isinstance(node,list):
            return len(node)>1 or any(has_multiple_arrays(x) for x in node)
        if isinstance(node,dict):
            return any(has_multiple_arrays(v) for v in node.values())
        return False
    out['rq1_instance_scope']=('multiple_instances_not_proven' if
        has_multiple_arrays(mapped_result['configs'][0]) else 'no_multiple_items_observed')
    if not selected(full.get(point)):
        status='point_not_selected_in_full'
    elif len(chosen_full)!=1:
        status='full_not_exactly_one'
    elif len(chosen_yaml)!=1:
        status='yaml_not_exactly_one'
    elif chosen_full!=chosen_yaml:
        status='different_variant'
    elif point not in mapped_result['selected_features']:
        status='yaml_parent_missing'
    else:
        status='match'
    out['rq1_vp_fidelity_status']=status
    return out


def audit_rq2(plain_docs, plain_path, documented_path, model, plan_row, rq1, full_path):
    out={k:'' for k in AUDIT_COLS}
    if documented_path is None or not documented_path.is_file():
        out['rq2_documentation_status']='documented_file_missing'
        return out
    try:
        raw=documented_path.read_text(encoding='utf-8-sig')
        doc_docs=list(yaml.safe_load_all(raw))
    except (OSError,yaml.YAMLError) as exc:
        out['rq2_documentation_status']='documented_parse_error'
        out['rq2_comment_detail']=str(exc)[:500]
        return out
    same=strict_tree(plain_docs)==strict_tree(doc_docs)
    out['rq2_data_equivalent']=same
    if not same:
        out['rq2_documentation_status']='data_changed'
        return out
    point=rq1.get('rq1_point_feature') or ''
    if not point:
        out['rq2_documentation_status']='data_equal_base_not_scored'
        return out
    feat,rel=get_point_relation(model,point)
    if rel is None:
        out['rq2_documentation_status']='data_equal_vp_unknown'
        return out
    full_selected=rq1.get('rq1_variant_full','').split(';') if rq1.get('rq1_variant_full') else []
    if len(full_selected)!=1:
        out['rq2_documentation_status']='data_equal_selected_variant_unavailable'
        return out
    chosen=full_selected[0]
    header=f'[Variability Point: {normalized_doc(feat) or feat.name}]'
    candidates=[block for block in comment_blocks(raw) if block and block[0]==header]
    if not candidates:
        out['rq2_vp_block_status']='missing'
        out['rq2_documentation_status']='vp_block_missing'
        return out
    if len(candidates)>1:
        out['rq2_vp_block_status']='ambiguous_multiple_blocks'
        out['rq2_documentation_status']='ambiguous_multiple_blocks'
        return out
    out['rq2_vp_block_status']='present'
    block=candidates[0]
    chosen_description=comment_variant_description(model['features'][chosen])
    selected_line=f'- Selected Variant: {chosen_description}'
    default_selected_line=f'- Selected Variant (default): {chosen_description}'
    selected_ok=(selected_line in block or default_selected_line in block)
    out['rq2_selected_comment_status']='correct' if selected_ok else 'missing_or_incorrect'
    expected=[f'- Alternative Variant: {comment_variant_description(c)}'
              for c in rel.children if c.name!=chosen]
    verified=sum(line in block for line in expected)
    out['rq2_alternatives_expected']=len(expected)
    out['rq2_alternatives_verified']=verified
    child=model['features'][chosen]
    has_default=any(a.name=='default' for a in child.get_attributes())
    out['rq2_selected_variant_has_default_metadata']=has_default
    # Existing generator prints 'Selected Variant', not 'Selected Default'.
    # Distinguish documenting selected value from explicitly stating default status.
    if has_default:
        # Accept a machine-identifiable marker; a narrative doc mentioning
        # the word `default` is NOT sufficient evidence of explicit labeling.
        out['rq2_default_explicitly_labeled'] = (
            'correct' if default_selected_line in block else 'not_expressed_by_current_format')
    if selected_ok and verified==len(expected):
        out['rq2_documentation_status']='checked_match'
    else:
        out['rq2_documentation_status']='comment_mismatch'
        missing=([selected_line] if not selected_ok else [])+[line for line in expected if line not in block]
        out['rq2_comment_detail']=' | '.join(missing)[:1000]
    return out


def run(args):
    if yaml is None:
        raise RuntimeError('PyYAML missing. pip install pyyaml')
    if args.uvl and not args.root_feature:
        raise ValueError('With --uvl you must set --root-feature')
    args.output_dir=args.output_dir.resolve()
    args.manifest_dir=args.manifest_dir.resolve()
    if args.cases_json is not None:
        args.cases_json=args.cases_json.resolve()
        if not args.cases_json.is_file():
            raise ValueError(f'Cases JSON not found: {args.cases_json}')
        if args.mapper is None:
            raise ValueError('--cases-json RQ3 checking requires --mapper, --descriptions and --metadata')
    if args.audit_plan_csv is not None or args.audit_generation_csv is not None:
        if not (args.audit_plan_csv and args.audit_generation_csv and
                args.uvl and args.full_config_dir and args.mapper):
            raise ValueError('Audits require --audit-plan-csv, --audit-generation-csv, --uvl, --full-config-dir and --mapper/metadata/descriptions')
    if args.full_config_dir is not None:
        args.full_config_dir = args.full_config_dir.resolve()
    if args.partial_config_dir is not None:
        args.partial_config_dir = args.partial_config_dir.resolve()
        if not args.partial_config_dir.is_dir():
            raise ValueError(f'Original partial-config directory not found: {args.partial_config_dir}')
    if args.full_config_dir and not args.uvl:
        raise ValueError('--full-config-dir requires --uvl and --root-feature')
    if args.fm_sat_mode != 'none' and not args.uvl:
        raise ValueError('--fm-sat-mode requires --uvl and --root-feature')
    mapper_params = (getattr(args, 'mapper', None), getattr(args, 'descriptions', None), args.metadata)
    if (getattr(args, 'mapper', None) is None) != (getattr(args, 'descriptions', None) is None):
        raise ValueError('--mapper and --descriptions must be supplied together')
    if args.mapper is not None and args.metadata is None:
        raise ValueError('--mapper requires --metadata')
    if args.fm_sat_mode == 'yaml' and args.mapper is None:
        raise ValueError('--fm-sat-mode yaml requires --mapper, --descriptions and --metadata')
    if args.fm_sat_mode == 'auto' and args.mapper is None and args.full_config_dir is None:
        raise ValueError('--fm-sat-mode auto requires mapper input and/or --full-config-dir')
    if args.kubeconform and not args.kubeconform_k8s_version:
        raise ValueError('--kubeconform requires --kubeconform-k8s-version for reproducible experiments')
    plans=load_case_table(args.audit_plan_csv)
    generations=load_case_table(args.audit_generation_csv)
    prompt_cases=load_cases_json(args.cases_json)
    manifests=sorted({*args.manifest_dir.rglob('*.yaml'),*args.manifest_dir.rglob('*.yml')})
    if not manifests:
        raise ValueError(f'No YAML in {args.manifest_dir}')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    default_audit_csv = args.output_dir / 'audit_default_values.csv'
    csv_path=args.output_dir/'evaluation_metrics.csv'
    core_csv_path=args.output_dir/'evaluation_core.csv'
    default_detail_cols = ['case_id','feature','occurrence','default_value','yaml_value','match','source']
    save_csv(default_audit_csv, [], default_detail_cols)
    save_csv(csv_path, [], COLS)
    save_csv(core_csv_path, [], CORE_COLS)
    model_t=time.perf_counter()
    model=index_uvl(args.uvl,args.root_feature) if args.uvl else None
    index=read_metadata(args.metadata) if args.metadata else None
    mapper = load_metadata_mapper(args.mapper, args.descriptions, args.metadata,
                                  strict=getattr(args, 'mapping_strict', False)) if args.mapper else None
    sat_validator=FMSATValidator(model['fm']) if args.fm_sat_mode != 'none' else None
    init_s=round(time.perf_counter()-model_t,6)
    rows=[]
    audit_rq1_rows=[]
    audit_rq2_rows=[]
    for i,path in enumerate(manifests,1):
        row={k:'' for k in COLS}
        row.update(case_id=path.stem,source_id=args.source_id,
                   manifest_yaml=str(path.resolve()),
                   hallucinations_status='not_evaluated',
                   validation_status='not_run',
                   schema_validation_status='not_run',fm_sat_status='not_run',
                   yaml_mapping_status='not_run',
                   rq3_requirement_status='not_run' if args.cases_json is None else 'pending',
                   status='started')
        tcase=time.perf_counter()
        try:
            t=time.perf_counter()
            raw=path.read_text(encoding='utf-8-sig')
            row['time_read_yaml_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            try:
                docs=list(yaml.safe_load_all(raw))
            except yaml.YAMLError as e:
                row.update(status='invalid_yaml',yaml_syntax_valid=False,error=str(e)[:1800])
                docs=[]
            row['time_yaml_parse_s']=round(time.perf_counter()-t,6)
            if row['status']=='invalid_yaml':
                continue
            row['yaml_syntax_valid']=True
            row['yaml_documents']=len(docs)
            if not docs or not all(isinstance(d,dict) for d in docs):
                row.update(status='invalid_document',error='YAML docs must be non-empty mappings')
                continue
            if len(docs)==1:
                row.update(yaml_kind=docs[0].get('kind',''),yaml_api_version=docs[0].get('apiVersion',''))
            if args.expected_kind or args.expected_api_version:
                row['yaml_expected_gvk_ok']=all(
                    (not args.expected_kind or d.get('kind')==args.expected_kind) and
                    (not args.expected_api_version or d.get('apiVersion')==args.expected_api_version)
                    for d in docs)
            t=time.perf_counter()
            combined=[]
            totals=Counter()
            for d in docs:
                occ,counts=walk_yaml(d)
                combined.extend(occ)
                totals.update(counts)
            row.update(yaml_distinct_paths=len({p for p,_ in combined}),
                       yaml_scalar_occurrences=totals['scalars'],
                       yaml_list_item_occurrences=totals['list_items'],
                       yaml_object_occurrences=totals['objects'])
            if args.cases_json is not None:
                row.update(rq3_target_check(prompt_cases.get(path.stem), combined, mapped_result=None, mapping_status='not_run'))
            matched=None
            if index is not None:
                # Diagnostic path coverage ONLY. Do not infer feature selections
                # from these path matches; use MetadataMapper below.
                _,mp,up=match_metadata(combined,index,model['names'] if model else None)
                row.update(yaml_metadata_matched_paths=len(mp),
                           yaml_metadata_unmatched_paths=len(up),
                           yaml_metadata_match_pct=round(100*len(mp)/(len(mp)+len(up)),3) if mp or up else '')
            row['time_structure_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            mapped_result=None
            if mapper is not None:
                try:
                    mapped_result=convert_yaml_with_mapper(
                        mapper, docs, args.output_dir/'mapped_configs', path.stem, model=model)
                    matched=mapped_result['selected_features']
                    unknown_features=(matched - model['names']) if model is not None else set()
                    mapping_status=('mapped_with_uvl_mismatch' if unknown_features
                                    else 'mapped_with_unmapped_paths' if mapped_result['unmapped']
                                    else 'mapped')
                    row.update(yaml_mapping_status=mapping_status,
                               yaml_mapped_config_json=str(mapped_result['mapped_path'].resolve()),
                               yaml_mapper_unmapped=len(mapped_result['unmapped']),
                               yaml_mapper_ignored=len(mapped_result['ignored']),
                               yaml_matched_features=len(matched - unknown_features),
                               yaml_mapping_detail=json.dumps({'unmapped':mapped_result['unmapped'][:15],
                                    'ignored':mapped_result['ignored'][:10],
                                    'not_in_uvl':sorted(unknown_features)[:15]},
                                    ensure_ascii=False)[:2000])
                    if args.cases_json is not None:
                        row.update(rq3_target_check(prompt_cases.get(path.stem), combined, mapped_result=mapped_result, mapping_status=mapping_status))
                except Exception as exc:
                    row.update(yaml_mapping_status='mapping_error',
                               yaml_mapping_detail=f'{type(exc).__name__}: {exc}'[:2000])
                    if args.cases_json is not None:
                        row.update(rq3_target_check(prompt_cases.get(path.stem), combined, mapped_result=None, mapping_status='mapping_error'))
            row['time_yaml_to_fm_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            full=args.full_config_dir/f'{path.stem}.json' if args.full_config_dir else None
            compute_uvl_metrics(row,model,full,matched)
            partial = args.partial_config_dir / f'{path.stem}.json' if args.partial_config_dir else None
            default_detail, default_metrics = audit_default_values(row, model, mapped_result, full, partial)
            row.update(default_metrics)
            for detail in default_detail:
                append_csv_row(default_audit_csv, {'case_id': path.stem, **detail}, default_detail_cols)
            row['time_model_metrics_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            if args.kubeconform:
                row.update(kubeconform_check(path,args.kubeconform_k8s_version,
                                             args.kubeconform_timeout, args.kubeconform_schema_location,
                                              args.kubeconform_cache_dir))
            row['time_kubeconform_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            row.update(fm_sat_check(args.fm_sat_mode,sat_validator,model,full,matched,mapped_result))
            if args.audit_plan_csv:
                t_audit=time.perf_counter()
                plan_row=plans.get(path.stem)
                gen_row=generations.get(path.stem, {})
                if plan_row is None:
                    row['rq1_vp_fidelity_status']='case_not_in_plan'
                    row['rq2_documentation_status']='case_not_in_plan'
                else:
                    rq1=audit_rq1(plan_row,gen_row,model,full,mapped_result)
                    row.update(rq1)
                    documented_name=gen_row.get('documented_manifest_yaml') or ''
                    documented_path=Path(documented_name).resolve() if documented_name else None
                    rq2=audit_rq2(docs,path,documented_path,model,plan_row,rq1,full)
                    row.update({k:v for k,v in rq2.items() if k.startswith('rq2_')})
                row['time_audit_s']=round(time.perf_counter()-t_audit,6)
            row['time_fm_sat_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            row.update(kubectl_check(path,args.kubectl_mode,args.kubectl_timeout))
            row['time_validation_s']=round(time.perf_counter()-t,6)
            if row['yaml_expected_gvk_ok'] is False:
                row['status']='gvk_mismatch'
            elif row['validation_status']=='rejected':
                row['status']='kubernetes_rejected'
            else:
                row['status']='evaluated'
        except Exception as e:
            row.update(status='analysis_error',error=f'{type(e).__name__}: {e}'[:1800])
        finally:
            row['time_evaluation_total_s']=round(time.perf_counter()-tcase,6)
            rows.append(row)
            append_csv_row(csv_path,row,COLS)
            append_csv_row(core_csv_path,row,CORE_COLS)
            print(f'[{i}/{len(manifests)}] {path.name}: {row["status"]}',flush=True)
    if sat_validator is not None:
        sat_validator.close()
    if args.audit_plan_csv:
        # Write one row per case in the original plan, including UNSAT / blocked cases
        # without a YAML. These do not contribute to fidelity denominators.
        by_case={r['case_id']:r for r in rows}
        for case_id,plan_row in plans.items():
            if case_id in by_case:
                original=by_case[case_id]
                a=dict(case_id=case_id,plan_status=plan_row.get('status',''),
                       generation_status=generations.get(case_id,{}).get('status',''))
                a.update({k:original.get(k,'') for k in AUDIT_COLS})
            else:
                g_status=generations.get(case_id,{}).get('status','')
                classification=('blocked_by_planner' if plan_row.get('status')=='blocked'
                                else 'unsat' if g_status=='unsat'
                                else 'generation_failed' if g_status=='failed'
                                else 'not_generated')
                a=dict(case_id=case_id,plan_status=plan_row.get('status',''),
                       generation_status=g_status,rq1_point_feature=plan_row.get('point_feature',''),
                       rq1_vp_fidelity_status=classification,
                       rq2_documentation_status='not_applicable_no_yaml')
            audit_rq1_rows.append({k:v for k,v in a.items() if k not in {
                'rq2_data_equivalent','rq2_documentation_status','rq2_vp_block_status',
                'rq2_selected_comment_status','rq2_alternatives_expected',
                'rq2_alternatives_verified','rq2_comment_detail',
                'rq2_selected_variant_has_default_metadata',
                'rq2_default_explicitly_labeled'}})
            audit_rq2_rows.append({k:v for k,v in a.items() if k not in {
                'rq1_variant_full','rq1_variant_yaml','rq1_vp_fidelity_status',
                'rq1_instance_scope','rq1_audit_detail'}})
        for name, audit_rows in (('audit_rq1_vp_fidelity.csv', audit_rq1_rows),
                                  ('audit_rq2_documentation.csv',audit_rq2_rows)):
            columns=list(dict.fromkeys(k for r in audit_rows for k in r))
            save_csv(args.output_dir/name,audit_rows,columns)
    summary={'source_id':args.source_id,'manifest_dir':str(args.manifest_dir.resolve()),
             'evaluated_files':len(rows),'status_counts':dict(Counter(r['status'] for r in rows)),
             'kubectl_mode':args.kubectl_mode,
             'kubeconform':args.kubeconform,
             'kubeconform_k8s_version':args.kubeconform_k8s_version,
             'fm_sat_mode':args.fm_sat_mode,'uvl':str(args.uvl) if args.uvl else None,
             'metadata':str(args.metadata) if args.metadata else None,
             'mapper':str(args.mapper) if args.mapper else None,
             'descriptions':str(args.descriptions) if args.descriptions else None,
             'mapping_strict':getattr(args,'mapping_strict',False),
             'full_config_dir':str(args.full_config_dir) if args.full_config_dir else None,
             'partial_config_dir':str(args.partial_config_dir) if args.partial_config_dir else None,
             'cases_json':str(args.cases_json) if args.cases_json else None,
             'default_value_audit_csv':str(default_audit_csv.resolve()),
             'time_model_init_s':init_s,'python_version':sys.version.split()[0],
             'platform':platform.platform(),
             'metric_note':'default indicates preferred feature selection; defaultValue comparison measures YAML values equal to reference defaults, not proven generator provenance. n_defaults_observed_total is a distinct-feature union, available only with full configuration. Optional partial-config analysis infers fallback but does not prove it. SAT-YAML projects schema-typed Boolean values (including false) to presence assumptions, retaining the original values in mapped JSON. n_variants = options in active alternative groups. n_features is exact only with FM completed JSON. yaml_matched_features uses MetadataMapper YAML->UVL but may be partial (unmapped reported). SAT (yaml mode) tests satisfiable extension of mapped hypotheses only; collection instances not independently modeled. RQ3 satisfaction uses cases.json point_feature against MetadataMapper selected_features; config_path presence is auxiliary evidence and variant_features report the observed alternative(s). schema_valid=Kubeconform; semantic_valid=kubectl server dry-run. Unmeasured is blank.',
             'output_csv':str(csv_path.resolve()),
             'output_core_csv':str(core_csv_path.resolve())}
    summary['fm_sat_status_counts'] = dict(Counter(r.get('fm_sat_status') or 'not_run' for r in rows))
    summary['schema_validation_status_counts'] = dict(Counter(r.get('schema_validation_status') or 'not_run' for r in rows))
    summary['rq1_vp_fidelity_status_counts'] = dict(Counter(r.get('rq1_vp_fidelity_status') or 'not_run' for r in rows))
    summary['rq2_documentation_status_counts'] = dict(Counter(r.get('rq2_documentation_status') or 'not_run' for r in rows))
    summary['default_value_audit_status_counts'] = dict(Counter(r.get('default_value_audit_status') or 'not_run' for r in rows))
    summary['rq3_requirement_status_counts'] = dict(Counter(r.get('rq3_requirement_status') or 'not_run' for r in rows))
    summary['rq3_point_feature_mapped_counts'] = dict(Counter(str(r.get('rq3_point_feature_mapped')) if r.get('rq3_point_feature_mapped') != '' else 'not_run' for r in rows))
    default_metric_names = (
        'n_defaults', 'n_defaults_observed_total',
        'n_default_value_features_selected','n_default_value_features_matching',
        'n_default_value_features_differing','n_default_value_occurrences_matching',
        'n_default_value_occurrences_differing',
        'n_default_value_inferred_applied','n_default_value_explicit_equal',
        'n_default_value_explicit_overrides')
    summary['default_metrics_aggregated'] = {
        name: (sum(int(r[name]) for r in rows if r.get(name) != '')
               if any(r.get(name) != '' for r in rows) else None)
        for name in default_metric_names
    }
    (args.output_dir/'evaluation_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Evaluation CSV: {csv_path} | Resumen: {core_csv_path}',flush=True)
    return 1 if any(r['status'] in ('invalid_yaml','invalid_document','analysis_error') for r in rows) else 0


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest-dir',required=True,type=Path)
    p.add_argument('--output-dir',required=True,type=Path)
    p.add_argument('--source-id',default='unspecified')
    p.add_argument('--expected-kind')
    p.add_argument('--expected-api-version')
    p.add_argument('--cases-json','--cases',dest='cases_json',type=Path,
                   help='Optional prompt/benchmark cases JSON; RQ3 checks point_feature against MetadataMapper selected_features and uses config_path as auxiliary evidence')
    p.add_argument('--uvl',type=Path,help='Optional FM for reference counts')
    p.add_argument('--root-feature')
    p.add_argument('--metadata',type=Path,help='Optional serialization_metadata JSON; needed for real YAML->UVL mapping')
    p.add_argument('--descriptions',type=Path,help='Descriptions JSON required with --mapper')
    p.add_argument('--mapper',type=Path,help='Path to metadata_parser01.py for authoritative YAML->UVL mapping')
    p.add_argument('--mapping-strict',action='store_true',help='Fail mapping on any unmapped path; default records unmapped paths')
    p.add_argument('--partial-config-dir',type=Path,
                   help='Optional ORIGINAL partial FM JSONs for inferred defaultValue provenance; not required')
    p.add_argument('--full-config-dir',type=Path,
                   help='Optional COMPLETED UVL JSONs indexed by YAML stem; exact FM counts only for these')
    p.add_argument('--audit-plan-csv',type=Path,
                   help='Optional: E2-B generation_results.csv to audit selected VP (RQ1) and requested-VP comments (RQ2)')
    p.add_argument('--audit-generation-csv',type=Path,
                   help='Optional: generation_times.csv, including documented_manifest_yaml paths')
    p.add_argument('--kubectl-mode',choices=('none','client','server'),default='none')
    p.add_argument('--kubeconform',action='store_true',help='Validate JSON Schema via kubeconform')
    p.add_argument('--kubeconform-k8s-version',help='Pin Kubernetes schema version, e.g. 1.30.0')
    p.add_argument('--kubeconform-schema-location',help='Optional offline custom schema location')
    p.add_argument('--kubeconform-timeout',type=int,default=45)
    p.add_argument('--kubeconform-cache-dir',type=Path,
                   help='Cache downloaded Kubernetes schemas to speed up repeated checks')
    p.add_argument('--fm-sat-mode',choices=('none','full','yaml','auto'),default='none',
                   help='Check SAT extension using full_config or MetadataMapper-produced YAML configuration')
    p.add_argument('--kubectl-timeout',type=int,default=45)
    a=p.parse_args(argv)
    if a.kubectl_timeout<=0 or a.kubeconform_timeout<=0:
        p.error('validation timeouts must be positive')
    return a

if __name__=='__main__':
    try:
        sys.exit(run(parse_args()))
    except Exception as e:
        print(f'ERROR: {type(e).__name__}: {e}',file=sys.stderr)
        sys.exit(2)