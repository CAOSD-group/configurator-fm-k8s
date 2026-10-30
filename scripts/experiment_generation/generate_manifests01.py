#!/usr/bin/env python3
"""Etapa 1 v2: generar YAML desde JSON parciales y medir SOLO la generación.

No valida Kubernetes, no calcula VP/defaults ni inspecciona la semántica YAML.
En el modo point-only, materializa en arrays la variante elegida por SAT
sin introducirla en el JSON parcial original.
Procesa TODOS los JSON de --partial-dir por defecto (sin --limit).
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import hashlib
import importlib.util
import json
import platform
import sys
import time
from collections import Counter
from pathlib import Path

COLS = [
    'case_id','partial_json','variation_point','requested_value','choice_feature',
    'full_config_json','manifest_yaml','documented_manifest_yaml','status','error',
    'sat_partial_ok','completion_iterations','sat_checks_in_completion','maxsat_calls',
    'time_partial_read_s','time_sat_precheck_s','time_completion_total_s',
    'time_defaults_s','time_sat_checks_in_completion_s','time_maxsat_s',
    'time_completion_other_s','array_points_resolved','time_resolve_array_points_s',
    'n_sat_forced_array_features','time_sat_forced_array_s',
    'n_selected_array_features_not_forced',
    'n_array_forced_merged','n_array_forced_already_present',
    'n_array_forced_deferred','n_array_forced_ambiguous',
    'n_array_forced_other_issues','n_materialization_events',
    'array_forced_issues_json','nonforced_array_features_json',
    'time_render_yaml_s','time_build_comments_s',
    'time_render_documented_s','time_write_s','time_write_documented_s',
    'time_case_total_s',
]


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load_module(path):
    path=path.resolve()
    spec=importlib.util.spec_from_file_location('k8s_generator_for_experiment',str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f'Cannot load generator {path}')
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod


def load_plan(path):
    if not path or not path.exists():
        return {}
    with path.open(encoding='utf-8-sig',newline='') as f:
        return {r['case_id']:r for r in csv.DictReader(f) if r.get('case_id')}


def write_csv(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=COLS,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def selected(value):
    return value is not False and value is not None


@contextlib.contextmanager
def instrument(gen, timings):
    """Monkey-patching local al experimento; no modifica el generador en disco.

    SAT interno se solapa con el tiempo default-aware: NO sumar ambos.
    """
    old_defaults=gen.generate_default_configuration
    old_sat=gen.SatChecker
    old_minimum=gen.PySATMinimumConfiguration

    def timed_defaults(*args,**kwargs):
        timings['completion_iterations']+=1
        t=time.perf_counter()
        try:
            return old_defaults(*args,**kwargs)
        finally:
            timings['time_defaults_s']+=time.perf_counter()-t

    class TimedSat(old_sat):
        def is_satisfiable(self,elements):
            timings['sat_checks_in_completion']+=1
            t=time.perf_counter()
            try:
                return super().is_satisfiable(elements)
            finally:
                timings['time_sat_checks_in_completion_s']+=time.perf_counter()-t

    class TimedMinimum(old_minimum):
        def execute(self,*args,**kwargs):
            timings['maxsat_calls']+=1
            t=time.perf_counter()
            try:
                return super().execute(*args,**kwargs)
            finally:
                timings['time_maxsat_s']+=time.perf_counter()-t

    gen.generate_default_configuration=timed_defaults
    gen.SatChecker=TimedSat
    gen.PySATMinimumConfiguration=TimedMinimum
    try:
        yield
    finally:
        gen.generate_default_configuration=old_defaults
        gen.SatChecker=old_sat
        gen.PySATMinimumConfiguration=old_minimum


def materialize_selected_array_alternatives(collections, all_features, full_elements):
    """Resolve a selected alternative *only in the render-time collection payload*.

    The input partial intentionally has {VP: true}; SAT/MaxSAT decides its child.
    main_k8s05 serializes explicit collection occurrences verbatim in compact
    mode, so {VP: true} would otherwise become a spurious `fsType: true`.

    This function is read-only with respect to the input JSON and `full_elements`.
    It returns (updated_collections, number_of_resolved_points).
    """
    changed = [0]

    def walk(item):
        if isinstance(item, list):
            return [walk(x) for x in item]
        if not isinstance(item, dict):
            return item
        out = {}
        for name, value in item.items():
            feature = all_features.get(name)
            if feature is None:
                raise ValueError(f'Unknown feature in collection: {name}')
            if value is True:
                groups = [r for r in feature.get_relations() if r.is_alternative()]
                if groups:
                    if len(groups) != 1:
                        raise ValueError(f'Multiple alternative groups under {name}')
                    chosen = [c for c in groups[0].children
                              if full_elements.get(c.name) is not None
                              and full_elements.get(c.name) is not False]
                    if len(chosen) != 1:
                        raise ValueError(f'Alternative not uniquely chosen in full config: {name}: '
                                         f'{[c.name for c in chosen]}')
                    out[name] = {chosen[0].name: True}
                    changed[0] += 1
                    continue
            out[name] = walk(value)
        return out

    result = {name: walk(copy.deepcopy(value)) for name, value in collections.items()}
    return result, changed[0]


def under_root(feature, root):
    while feature is not None:
        if feature is root:
            return True
        feature=feature.get_parent()
    return False


def selected_array_features_not_forced(gen, full, partial, collections,
                                       features, sat_model, forced):
    """Selected array descendants neither decided nor entailed by SAT.

    Excludes explicit choices within item payloads, which the SAT-level partial
    configuration does not hold. A non-entailed choice is not necessarily a
    MaxSAT choice: defaults or other completion policies may also choose it.
    """
    chosen = set(gen.selected_items(full))
    explicit = set(partial.elements)

    def visit(value):
        if isinstance(value, dict):
            for name, child in value.items():
                if name in features:
                    explicit.add(name)
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    for payload in collections.values():
        visit(payload)

    extra = []
    for name in chosen:
        if name in explicit or name in forced or name not in sat_model.variables:
            continue
        node = features.get(name)
        if node is None:
            continue
        node = node.get_parent()
        while node is not None:
            if gen.get_serialization_attr(node, 'serializationKind') == 'array':
                extra.append(name)
                break
            node = node.get_parent()
    return sorted(extra)


def classify_forced_array_events(events):
    """Count only known, explicit merge outcomes; no claimed fidelity rate."""
    merged = present = deferred = ambiguous = other = 0
    issues = []
    for event in events:
        if ': merged into ' in event:
            merged += 1
        elif ': already present ' in event:
            present += 1
        elif ': deferred ' in event:
            deferred += 1
            issues.append(event)
        elif ': ambiguous ' in event:
            ambiguous += 1
            issues.append(event)
        else:
            other += 1
            issues.append(event)
    return merged, present, deferred, ambiguous, other, issues


def run(args):
    # Windows MAX_PATH: normalize before constructing deeply nested output paths
    # (especially documented_manifests/<long case_id>.yaml).
    args.output_dir = args.output_dir.resolve()
    cases=sorted(args.partial_dir.glob('*.json'))
    if args.limit:
        cases=cases[:args.limit]
    if not cases:
        raise ValueError(f'No JSONs found in {args.partial_dir}')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    csv_path=args.output_dir/'generation_times.csv'
    summary_path=args.output_dir/'generation_summary.json'
    fingerprint={
        'uvl_sha256':sha256(args.uvl),
        'generator_sha256':sha256(args.generator),
        'partial_dir':str(args.partial_dir.resolve()),
        'root_feature':args.root_feature,
        'variability_comments':args.variability_comments,
    }
    if args.resume and summary_path.exists():
        old_summary=json.loads(summary_path.read_text(encoding='utf-8'))
        if any(old_summary.get(k)!=v for k,v in fingerprint.items()):
            raise ValueError('Resume: UVL/generator/partial-dir/root/comments changed. Use a new output-dir.')
    old_rows=load_plan(csv_path) if args.resume else {}
    plan=load_plan(args.plan_csv or args.partial_dir.parent/'generation_results.csv')

    gen=load_module(args.generator)
    necessary=('UVLReader','FmToPysat','SatChecker','PySATMinimumConfiguration',
               'generate_default_configuration','load_configurations_with_collections',
               'complete_and_minimize','render_manifest','write_manifest',
               '_sat_forced_array_features')
    missing=[x for x in necessary if not hasattr(gen,x)]
    if missing:
        raise ValueError(f'Generator missing APIs: {missing}')
    globals_t={}
    t=time.perf_counter()
    fm=gen.UVLReader(str(args.uvl)).transform()
    globals_t['time_uvl_load_s']=round(time.perf_counter()-t,6)
    names={f.name:f for f in fm.get_features()}
    root=fm.get_feature_by_name(args.root_feature) if args.root_feature else None
    if args.root_feature and root is None:
        raise ValueError(f'Unknown UVL root feature: {args.root_feature}')
    scope={f.name for f in fm.get_features() if under_root(f,root)} if root else None
    t=time.perf_counter()
    sat_model=gen.FmToPysat(fm).transform()
    globals_t['time_sat_translation_s']=round(time.perf_counter()-t,6)
    t=time.perf_counter()
    checker=gen.SatChecker(sat_model)
    globals_t['time_sat_checker_init_s']=round(time.perf_counter()-t,6)

    rows=[]
    try:
        for index,partial_path in enumerate(cases,1):
            case_id=partial_path.stem
            previous=old_rows.get(case_id)
            if previous and previous.get('status')=='generated' and all(
                Path(previous.get(f) or '__missing__').is_file() for f in
                ('full_config_json','manifest_yaml')
            ) and (not args.variability_comments or
                    Path(previous.get('documented_manifest_yaml') or '__missing__').is_file()):
                rows.append(previous)
                continue
            meta=plan.get(case_id,{})
            row={k:'' for k in COLS}
            row.update(case_id=case_id, partial_json=str(partial_path.resolve()),
                       variation_point=meta.get('variation_point',''),
                       requested_value=meta.get('requested_value',''),
                       choice_feature=meta.get('choice_feature',''),status='started')
            metrics={k:0 for k in ('completion_iterations','sat_checks_in_completion','maxsat_calls')}
            metrics.update({k:0.0 for k in ('time_defaults_s','time_sat_checks_in_completion_s','time_maxsat_s')})
            full_path=args.output_dir/'full_configs'/f'{case_id}.json'
            manifest_path=args.output_dir/'manifests'/f'{case_id}.yaml'
            documented_path=args.output_dir/'documented_manifests'/f'{case_id}.yaml'
            for p in (full_path,manifest_path,documented_path):
                p.unlink(missing_ok=True)
            total_start=time.perf_counter()
            stage='read_partial'
            try:
                t=time.perf_counter()
                data=json.loads(partial_path.read_text(encoding='utf-8-sig'))
                content=data.get('config',data)
                if not isinstance(content,dict):
                    raise ValueError('Partial JSON must be an object')
                unknown=set(content)-set(names)
                if unknown:
                    raise ValueError(f'Unknown features: {sorted(unknown)[:12]}')
                if scope is not None:
                    outside=set(content)-scope
                    if outside:
                        raise ValueError(f'Features outside resource subtree: {sorted(outside)[:12]}')
                partial,collections=gen.load_configurations_with_collections(fm,[str(partial_path)])
                row['time_partial_read_s']=round(time.perf_counter()-t,6)

                stage='sat_precheck'
                t=time.perf_counter()
                sat=checker.is_satisfiable(partial.elements)
                row['time_sat_precheck_s']=round(time.perf_counter()-t,6)
                row['sat_partial_ok']=sat
                if not sat:
                    row['status']='unsat'
                    raise ValueError('Partial decisions are UNSAT')

                stage='completion'
                t=time.perf_counter()
                try:
                    with instrument(gen,metrics):
                        full=gen.complete_and_minimize(fm,sat_model,partial,expand_to_leaves=False)
                finally:
                    row['time_completion_total_s']=round(time.perf_counter()-t,6)

                stage='resolve_array_points'
                t=time.perf_counter()
                try:
                    render_collections, resolved_count = materialize_selected_array_alternatives(
                        collections, names, full.elements)
                    row['array_points_resolved'] = resolved_count
                finally:
                    row['time_resolve_array_points_s'] = round(time.perf_counter()-t,6)

                stage='sat_forced_array_features'
                t=time.perf_counter()
                try:
                    forced_array_features=gen._sat_forced_array_features(
                        fm,sat_model,partial,full,collections=render_collections,
                        checker=checker)
                    row['n_sat_forced_array_features']=len(forced_array_features)
                    selected_not_forced = selected_array_features_not_forced(
                        gen, full, partial, render_collections, names, sat_model,
                        forced_array_features)
                    row['n_selected_array_features_not_forced'] = len(selected_not_forced)
                    row['nonforced_array_features_json'] = json.dumps(
                        selected_not_forced, ensure_ascii=False)
                finally:
                    row['time_sat_forced_array_s']=round(time.perf_counter()-t,6)

                stage='render_yaml'
                t=time.perf_counter()
                rendering_report = {}
                yaml_text=gen.render_manifest(
                    fm,full,None,collections=render_collections,
                    expand_to_leaves=False,collection_mode='empty',
                    forced_array_features=forced_array_features,
                    serialization_diagnostics=rendering_report)
                events = rendering_report.get('forced_array_events', [])
                (merged, present, deferred, ambiguous, other,
                 issues) = classify_forced_array_events(events)
                row.update(n_array_forced_merged=merged,
                           n_array_forced_already_present=present,
                           n_array_forced_deferred=deferred,
                           n_array_forced_ambiguous=ambiguous,
                           n_array_forced_other_issues=other,
                           n_materialization_events=len(rendering_report.get('materialization_events', [])),
                           array_forced_issues_json=json.dumps(issues,ensure_ascii=False))
                row['time_render_yaml_s']=round(time.perf_counter()-t,6)
                if not yaml_text or not yaml_text.strip():
                    raise ValueError('Empty rendered YAML')

                if args.variability_comments:
                    stage='comments'
                    t=time.perf_counter()
                    comment_data=gen.build_variability_comments(
                        fm,full,set(gen.selected_items(partial)))
                    row['time_build_comments_s']=round(time.perf_counter()-t,6)
                    stage='render_documented'
                    t=time.perf_counter()
                    documented=gen.render_manifest(
                        fm,full,comment_data,collections=render_collections,
                        expand_to_leaves=False,collection_mode='empty',
                        forced_array_features=forced_array_features)
                    row['time_render_documented_s']=round(time.perf_counter()-t,6)

                stage='write_yaml'
                t=time.perf_counter()
                full_path.parent.mkdir(parents=True,exist_ok=True)
                full_path.write_text(json.dumps(full.elements,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
                gen.write_manifest(yaml_text,manifest_path)
                row['time_write_s']=round(time.perf_counter()-t,6)
                row['full_config_json']=str(full_path.resolve())
                row['manifest_yaml']=str(manifest_path.resolve())
                if args.variability_comments:
                    stage='write_documented'
                    t=time.perf_counter()
                    gen.write_manifest(documented,documented_path)
                    row['time_write_documented_s']=round(time.perf_counter()-t,6)
                    row['documented_manifest_yaml']=str(documented_path.resolve())
                row['status']='generated'
            except Exception as exc:
                if row['status']=='started':
                    row['status']='unsat' if 'not satisfiable' in str(exc).lower() or 'unsat' in str(exc).lower() else 'failed'
                row['error']=f'{stage}: {type(exc).__name__}: {exc}'[:1800]
            finally:
                row.update({k:round(v,6) if isinstance(v,float) else v for k,v in metrics.items()})
                if row['time_completion_total_s']!='':
                    row['time_completion_other_s']=round(max(0.0,row['time_completion_total_s']-
                        metrics['time_defaults_s']-metrics['time_maxsat_s']),6)
                row['time_case_total_s']=round(time.perf_counter()-total_start,6)
                rows.append(row)
                write_csv(csv_path,rows)
                print(f'[{index}/{len(cases)}] {case_id} -> {row["status"]} ({row["time_case_total_s"]:.3f}s)',flush=True)
    finally:
        checker.close()

    summary=dict(fingerprint,
        number_of_input_partials=len(cases),
        number_processed=len(rows),
        status_counts=dict(Counter(r['status'] for r in rows)),
        serialization_diagnostics_summary={
            'cases_with_forced_array_issues':sum(
                1 for r in rows if any(int(r.get(k) or 0) for k in
                    ('n_array_forced_deferred','n_array_forced_ambiguous',
                     'n_array_forced_other_issues'))),
            'total_forced_array_deferred':sum(int(r.get('n_array_forced_deferred') or 0) for r in rows),
            'total_forced_array_ambiguous':sum(int(r.get('n_array_forced_ambiguous') or 0) for r in rows),
            'total_forced_array_merged':sum(int(r.get('n_array_forced_merged') or 0) for r in rows),
            'total_selected_array_features_not_forced':sum(
                int(r.get('n_selected_array_features_not_forced') or 0) for r in rows),
        },
        global_times=globals_t,
        time_unit='seconds; perf_counter; UVL/SAT initialisation outside per-case timings',
        time_overlap_note=('time_sat_checks_in_completion_s INCLUDED in time_defaults_s; '
                           'time_sat_forced_array_s is a separate post-completion SAT phase'),
        python_version=sys.version.split()[0],platform=platform.platform(),
        output_csv=str(csv_path.resolve()))
    summary_path.write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(f'Generation CSV: {csv_path}\nSummary: {summary_path}',flush=True)
    return 1 if any(r['status']=='failed' for r in rows) else 0


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--partial-dir',type=Path,required=True)
    p.add_argument('--uvl',type=Path,required=True)
    p.add_argument('--generator',type=Path,required=True)
    p.add_argument('--root-feature',help='Exact UVL feature name; optional scope check')
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--plan-csv',type=Path,help='Optional CSV from --plan for experiment identity')
    p.add_argument('--variability-comments',action='store_true')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--limit',type=int,default=0,help='0=ALL. Optional debugging only')
    args=p.parse_args(argv)
    if args.limit<0:
        p.error('--limit must be nonnegative')
    return args

if __name__=='__main__':
    try:
        sys.exit(run(parse_args()))
    except Exception as e:
        print(f'ERROR: {type(e).__name__}: {e}',file=sys.stderr)
        sys.exit(2)