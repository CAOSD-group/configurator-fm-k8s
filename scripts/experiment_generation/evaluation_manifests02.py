#!/usr/bin/env python3
"""Etapa 2 v4: evaluar YAML de CUALQUIER origen (FM, LLM, manual, etc.).

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

COLS=[
    'case_id','source_id','manifest_yaml','full_config_json','status','error',
    'n_features','n_variation_points','n_variants','n_selected_variants',
    'n_defaults','pct_defaults',
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
    'uvl_defaults_available',
    'schema_valid','schema_validation_status','schema_validation_detail',
    'fm_sat_valid','fm_sat_status','fm_sat_basis','fm_sat_detail',
    'semantic_valid','validation_status','validation_detail',
    'hallucinations','hallucinations_status',
    'time_read_yaml_s','time_yaml_parse_s','time_structure_s','time_model_metrics_s',
    'time_validation_s','time_kubeconform_s','time_fm_sat_s',
    'time_evaluation_total_s',
    'yaml_mapping_status','yaml_mapping_detail','yaml_mapped_config_json',
    'yaml_mapper_unmapped','yaml_mapper_ignored',
    'time_yaml_to_fm_s',
]


# Resumen de métricas de investigación. El CSV completo conserva diagnósticos.
CORE_COLS = [
    'case_id','source_id','manifest_yaml','status',
    'n_features','n_variation_points','n_variants','n_selected_variants',
    'n_defaults','pct_defaults',
    'yaml_matched_features','yaml_matched_alternative_variants','yaml_matched_defaults',
    'yaml_mapping_status',
    'yaml_syntax_valid','schema_valid','schema_validation_status',
    'fm_sat_valid','fm_sat_status','fm_sat_basis',
    'semantic_valid','validation_status',
    'hallucinations','hallucinations_status',
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
    alt_children={c.name for vp,rel in groups for c in rel.children}
    return {'fm':fm,'features':features,'names':names,'groups':groups,'defaults':defaults,
            'alt_children':alt_children,
            'available_alternative_groups':len(groups),
            'available_alternative_variants':sum(len(rel.children) for _,rel in groups),
            'defaults_available':len(defaults)}


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


def ambiguous_collection_alternatives(hypotheses, model):
    """Multiple chosen children from one alternative group cannot be verified
    accurately without per-instance variables. Classify them as 'unavailable'."""
    if model is None:
        return []
    selected_features = {k for k, v in hypotheses.items() if selected(v)}
    return [vp.name for vp, relation in model['groups']
            if sum(child.name in selected_features for child in relation.children) > 1]


def convert_yaml_with_mapper(mapper, docs, output_dir, case_id):
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
    selected_features = {name for decisions in hypotheses_list for name, val in decisions.items()
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
        checked, detail = sat_validator.check(hypotheses)
        return {'fm_sat_status': 'passed' if checked else 'rejected' if checked is False else 'unavailable',
                'fm_sat_valid': checked if checked is not None else '',
                'fm_sat_basis':'metadata_mapper_partial_config',
                'fm_sat_detail': ('SAT checks an extension of mapped hypotheses, not YAML semantic validity. ' + detail).strip()}
    return {'fm_sat_status':'not_run','fm_sat_detail':'No usable full config or mapped YAML configuration'}


def kubeconform_check(path, version, timeout, schema_location=None):
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


def run(args):
    if yaml is None:
        raise RuntimeError('PyYAML missing. pip install pyyaml')
    if args.uvl and not args.root_feature:
        raise ValueError('With --uvl you must set --root-feature')
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
    manifests=sorted({*args.manifest_dir.rglob('*.yaml'),*args.manifest_dir.rglob('*.yml')})
    if not manifests:
        raise ValueError(f'No YAML in {args.manifest_dir}')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    csv_path=args.output_dir/'evaluation_metrics.csv'
    core_csv_path=args.output_dir/'evaluation_core.csv'
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
    for i,path in enumerate(manifests,1):
        row={k:'' for k in COLS}
        row.update(case_id=path.stem,source_id=args.source_id,
                   manifest_yaml=str(path.resolve()),
                   hallucinations_status='not_evaluated',
                   validation_status='not_run',
                   schema_validation_status='not_run',fm_sat_status='not_run',
                   yaml_mapping_status='not_run',status='started')
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
                        mapper, docs, args.output_dir/'mapped_configs', path.stem)
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
                except Exception as exc:
                    row.update(yaml_mapping_status='mapping_error',
                               yaml_mapping_detail=f'{type(exc).__name__}: {exc}'[:2000])
            row['time_yaml_to_fm_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            full=args.full_config_dir/f'{path.stem}.json' if args.full_config_dir else None
            compute_uvl_metrics(row,model,full,matched)
            row['time_model_metrics_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            if args.kubeconform:
                row.update(kubeconform_check(path,args.kubeconform_k8s_version,
                                             args.kubeconform_timeout, args.kubeconform_schema_location))
            row['time_kubeconform_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            row.update(fm_sat_check(args.fm_sat_mode,sat_validator,model,full,matched,mapped_result))
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
             'time_model_init_s':init_s,'python_version':sys.version.split()[0],
             'platform':platform.platform(),
             'metric_note':'n_variants = options in active alternative groups. n_features is exact only with FM completed JSON. yaml_matched_features uses MetadataMapper YAML->UVL but may be partial (unmapped reported). SAT (yaml mode) tests satisfiable extension of mapped hypotheses only; collection instances not independently modeled. schema_valid=Kubeconform; semantic_valid=kubectl server dry-run. Unmeasured is blank.',
             'output_csv':str(csv_path.resolve()),
             'output_core_csv':str(core_csv_path.resolve())}
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
    p.add_argument('--uvl',type=Path,help='Optional FM for reference counts')
    p.add_argument('--root-feature')
    p.add_argument('--metadata',type=Path,help='Optional serialization_metadata JSON; needed for real YAML->UVL mapping')
    p.add_argument('--descriptions',type=Path,help='Descriptions JSON required with --mapper')
    p.add_argument('--mapper',type=Path,help='Path to metadata_parser01.py for authoritative YAML->UVL mapping')
    p.add_argument('--mapping-strict',action='store_true',help='Fail mapping on any unmapped path; default records unmapped paths')
    p.add_argument('--full-config-dir',type=Path,
                   help='Optional COMPLETED UVL JSONs indexed by YAML stem; exact FM counts only for these')
    p.add_argument('--kubectl-mode',choices=('none','client','server'),default='none')
    p.add_argument('--kubeconform',action='store_true',help='Validate JSON Schema via kubeconform')
    p.add_argument('--kubeconform-k8s-version',help='Pin Kubernetes schema version, e.g. 1.30.0')
    p.add_argument('--kubeconform-schema-location',help='Optional offline custom schema location')
    p.add_argument('--kubeconform-timeout',type=int,default=45)
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
