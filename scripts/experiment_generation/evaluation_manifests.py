#!/usr/bin/env python3
"""Etapa 2: evaluar YAML de CUALQUIER origen (FM, LLM, manual, etc.).

No llama a la generacion, SAT ni MaxSAT. Los campos que no pueden medirse
se dejan vacios: vacio != 0, y 'no evaluado' != 'valido'.

--metadata permite estimar features observables en YAML mediante configPath;
--full-config-dir anade conteos exactos UVL cuando existen JSON completados
(estos ultimos NO son comparables directamente con casos solo-YAML).
"""
from __future__ import annotations

import argparse
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
    'schema_valid','semantic_valid','validation_status','validation_detail',
    'hallucinations','hallucinations_status',
    'time_read_yaml_s','time_yaml_parse_s','time_structure_s','time_model_metrics_s',
    'time_validation_s','time_evaluation_total_s',
]


def save_csv(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=COLS,extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


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
    return {'features':features,'names':names,'groups':groups,'defaults':defaults,
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


def compute_uvl_metrics(row,model,full_path,matched):
    if model is None:
        return
    row.update(uvl_available_alternative_groups=model['available_alternative_groups'],
               uvl_available_alternative_variants=model['available_alternative_variants'],
               uvl_defaults_available=model['defaults_available'])
    row['yaml_matched_alternative_variants']=len(matched & model['alt_children']) if matched is not None else ''
    row['yaml_matched_defaults']=len(matched & model['defaults']) if matched is not None else ''
    if not full_path or not full_path.is_file():
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
    row['full_config_json']=str(full_path.resolve())


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
                'schema_valid':True,'semantic_valid':True if mode=='server' else ''}
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
                'schema_valid':False,'semantic_valid':False if mode=='server' else ''}
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
    manifests=sorted({*args.manifest_dir.rglob('*.yaml'),*args.manifest_dir.rglob('*.yml')})
    if not manifests:
        raise ValueError(f'No YAML in {args.manifest_dir}')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    csv_path=args.output_dir/'evaluation_metrics.csv'
    model_t=time.perf_counter()
    model=index_uvl(args.uvl,args.root_feature) if args.uvl else None
    index=read_metadata(args.metadata) if args.metadata else None
    init_s=round(time.perf_counter()-model_t,6)
    rows=[]
    for i,path in enumerate(manifests,1):
        row={k:'' for k in COLS}
        row.update(case_id=path.stem,source_id=args.source_id,
                   manifest_yaml=str(path.resolve()),
                   hallucinations_status='not_evaluated',
                   validation_status='not_run',status='started')
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
                allowed=model['names'] if model else None
                matched,mp,up=match_metadata(combined,index,allowed)
                row.update(yaml_matched_features=len(matched),
                           yaml_metadata_matched_paths=len(mp),
                           yaml_metadata_unmatched_paths=len(up),
                           yaml_metadata_match_pct=round(100*len(mp)/(len(mp)+len(up)),3) if mp or up else '')
            row['time_structure_s']=round(time.perf_counter()-t,6)
            t=time.perf_counter()
            full=args.full_config_dir/f'{path.stem}.json' if args.full_config_dir else None
            compute_uvl_metrics(row,model,full,matched)
            row['time_model_metrics_s']=round(time.perf_counter()-t,6)
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
            save_csv(csv_path,rows)
            print(f'[{i}/{len(manifests)}] {path.name}: {row["status"]}',flush=True)
    summary={'source_id':args.source_id,'manifest_dir':str(args.manifest_dir.resolve()),
             'evaluated_files':len(rows),'status_counts':dict(Counter(r['status'] for r in rows)),
             'kubectl_mode':args.kubectl_mode,'uvl':str(args.uvl) if args.uvl else None,
             'metadata':str(args.metadata) if args.metadata else None,
             'full_config_dir':str(args.full_config_dir) if args.full_config_dir else None,
             'time_model_init_s':init_s,'python_version':sys.version.split()[0],
             'platform':platform.platform(),
             'metric_note':'YAML observable counts are source-neutral. Full UVL selected counts require full_config JSON. Metadata matches are conservative projections, NOT proven complete FM configurations. Unmeasured is blank.',
             'output_csv':str(csv_path.resolve())}
    (args.output_dir/'evaluation_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Evaluation CSV: {csv_path}',flush=True)
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
    p.add_argument('--metadata',type=Path,help='Optional serialization_metadata JSON to project YAML paths')
    p.add_argument('--full-config-dir',type=Path,
                   help='Optional COMPLETED UVL JSONs indexed by YAML stem; exact FM counts only for these')
    p.add_argument('--kubectl-mode',choices=('none','client','server'),default='none')
    p.add_argument('--kubectl-timeout',type=int,default=45)
    a=p.parse_args(argv)
    if a.kubectl_timeout<=0:
        p.error('--kubectl-timeout must be positive')
    return a

if __name__=='__main__':
    try:
        sys.exit(run(parse_args()))
    except Exception as e:
        print(f'ERROR: {type(e).__name__}: {e}',file=sys.stderr)
        sys.exit(2)