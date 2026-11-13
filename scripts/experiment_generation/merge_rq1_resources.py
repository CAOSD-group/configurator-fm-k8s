#!/usr/bin/env python3
"""Merge *case-level* RQ1 evaluations of Kubernetes Deployment and Pod.

Input per resource (four CSVs produced by the existing RQ1 runners):
  generation_times.csv, evaluation_metrics.csv,
  audit_rq1_vp_fidelity.csv, audit_rq2_documentation.csv

The data are pooled at manifest/case level. Count metrics are additive; medians,
P90s and relative overheads are computed on the pooled, correctly paired data.
CSV case IDs are unique only *within* a resource; pair by (resource,case_id).

Example:
 python merge_rq1_resources_caise.py \
  --dataset Deployment path/deploy/generation_times.csv path/deploy/evaluation_metrics.csv path/deploy/audit_rq1_vp_fidelity.csv path/deploy/audit_rq2_documentation.csv \
  --dataset Pod path/pod/generation_times.csv path/pod/evaluation_metrics.csv path/pod/audit_rq1_vp_fidelity.csv path/pod/audit_rq2_documentation.csv \
  --output-dir ./rq1_combined
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def read_csv(p: Path):
    if not p.is_file():
        raise FileNotFoundError(f'Missing CSV: {p}')
    with p.open('r', newline='', encoding='utf-8-sig') as f:
        r = csv.DictReader(f)
        if not r.fieldnames or 'case_id' not in r.fieldnames:
            raise ValueError(f'Missing case_id column: {p}')
        rows = list(r)
    ids = [r['case_id'] for r in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError(f'Empty or repeated case_id values: {p}')
    return rows


def flt(r, key):
    try:
        s=r.get(key, '')
        if s in ('', None):
            return None
        n=float(s)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def truthy(r, key):
    return str(r.get(key, '')).strip().lower() in ('true', '1', 'yes')


def sumint(rows, key):
    return sum(int(float(r[key])) for r in rows if r.get(key) not in ('', None))


def stats(values):
    if not values:
        return {'n':0, 'median':None,'p90':None,'q25':None,'q75':None,'mean':None}
    a=np.asarray(values, dtype=float)
    return {'n':len(values),'median':round(float(np.median(a)),6),
            'p90':round(float(np.percentile(a,90)),6),
            'q25':round(float(np.percentile(a,25)),6),
            'q75':round(float(np.percentile(a,75)),6),
            'mean':round(float(np.mean(a)),6)}


def summary(name, gen, ev, rq1, rq2, pairs):
    eval_ids={r['case_id'] for r in ev}
    gen_idx={r['case_id']:r for r in gen}
    rq1_idx={r['case_id']:r for r in rq1}
    rq2_idx={r['case_id']:r for r in rq2}
    point_rows=[r for r in rq1 if r.get('rq1_point_feature')]
    generated=[r for r in point_rows if r.get('generation_status')=='generated']
    unsat=[r for r in point_rows if r.get('generation_status')=='unsat' or r.get('rq1_vp_fidelity_status')=='unsat']
    validations=Counter(r.get('schema_validation_status') or 'not_run' for r in ev)
    sat=Counter(r.get('fm_sat_status') or 'not_run' for r in ev)
    alt_checked=[r for r in rq2 if r.get('rq2_documentation_status')=='checked_match']
    alts_expected=sumint(alt_checked, 'rq2_alternatives_expected')
    alts_verified=sumint(alt_checked, 'rq2_alternatives_verified')
    match=sum(r.get('rq1_vp_fidelity_status')=='match' for r in generated)
    missing_generated=[r['case_id'] for r in ev if r['case_id'] not in gen_idx]
    timing_ids={p['case_id'] for p in pairs}
    missing_timing=sorted(eval_ids-timing_ids)
    warnings=[]
    if missing_generated: warnings.append(f'{len(missing_generated)} evaluated cases absent from generation_times.csv')
    if missing_timing: warnings.append(f'{len(missing_timing)} evaluated cases lack paired timing')
    if alts_expected != alts_verified: warnings.append(f'Alternative comments: verified {alts_verified}/{alts_expected}')
    if match != len(generated): warnings.append(f'VP fidelity matching {match}/{len(generated)}')
    if len(ev) != len(generated)+1: warnings.append(f'Expected one baseline + generated VPs, got {len(ev)} vs {len(generated)}+1')
    # Pair-derived measures, not the difference of the two resource medians.
    fields=('plain','documented','incremental','comments','rendering_increment','writing_increment','relative_pct')
    timing={f:stats([r[f] for r in pairs if r[f] is not None]) for f in fields}
    return {
      'resource':name,'planned_vps':len(point_rows),'generated_vps':len(generated),
      'unsat_vps':len(unsat),
      'other_vp_outcomes':len(point_rows)-len(generated)-len(unsat),
      'generated_manifests':len(ev),
      'schema_passed':validations['passed'],'schema_rejected':validations['rejected'],
      'schema_other':len(ev)-validations['passed']-validations['rejected'],
      'sat_yaml_passed':sat['passed'],'sat_yaml_rejected':sat['rejected'],
      'sat_yaml_other':len(ev)-sat['passed']-sat['rejected'],
      'vp_variant_reconstructed':match,'vp_variant_compared':len(generated),
      'plain_documented_same_data':sum(truthy(r,'rq2_data_equivalent') for r in ev),
      'documentation_blocks_correct':len(alt_checked),
      'documentation_blocks_compared':len(generated),
      'alternative_descriptions_expected':alts_expected,
      'alternative_descriptions_verified':alts_verified,
      'default_marked_selected_unlabeled':sum(r.get('rq2_default_explicitly_labeled')=='not_expressed_by_current_format' for r in rq2),
      'paired_timings':len(pairs),
      'timing':timing,'warnings':warnings,
    }


def generate_fig(pairs, output_dir):
    fields=[('incremental','Total documentation overhead'),
            ('comments','Comment generation'),
            ('rendering_increment','Additional YAML rendering')]
    if not pairs:
        return
    available=[(key,label,[p[key] for p in pairs if p[key] is not None]) for key,label in fields]
    available=[row for row in available if row[2]]
    fig,ax=plt.subplots(figsize=(7.3,2.65))
    rng=np.random.default_rng(1137)
    colors=plt.rcParams['axes.prop_cycle'].by_key()['color']
    max_x=0
    for i,(key,label,v) in enumerate(available):
        arr=np.asarray(v,dtype=float)
        med=float(np.median(arr)); q1,q3=np.percentile(arr,[25,75]);p90=np.percentile(arr,90)
        jitter=rng.uniform(-0.12,0.12,size=len(arr))
        ax.scatter(arr,np.full(len(arr),i)+jitter,color=colors[i%len(colors)],s=7,alpha=.26,linewidths=0,rasterized=True)
        ax.hlines(i,q1,q3,color=colors[i%len(colors)],linewidth=4,alpha=.8)
        ax.vlines(med,i-.17,i+.17,color='black',linewidth=1.4)
        max_x=max(max_x,float(np.percentile(arr,99)))
        if key=='incremental':
            ax.text(med+0.025,i-.25,f'Median {med:.3f} s  |  P90 {p90:.3f} s',fontsize=8)
    ax.set_yticks(range(len(available)),[x[1] for x in available])
    ax.invert_yaxis()
    ax.set_xlabel('Additional time per manifest (s)')
    ax.set_xlim(left=min(0,min(p['incremental'] for p in pairs)-.04),right=max_x*1.23+.04)
    ax.grid(axis='x',alpha=.23)
    ax.spines[['top','right','left']].set_visible(False)
    ax.tick_params(axis='y',length=0)
    fig.tight_layout()
    for ext in ('pdf','png'):
        fig.savefig(output_dir/f'rq1_overhead_combined.{ext}',bbox_inches='tight',dpi=250 if ext=='png' else None)
    plt.close(fig)


def csv_out(path, rows):
    if not rows:
        return
    columns=list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,columns,extrasaction='ignore');w.writeheader();w.writerows(rows)


def main(argv=None):
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--dataset',action='append',nargs=5,
                   metavar=('RESOURCE','GENERATION_TIMES','EVALUATION_METRICS','AUDIT_RQ1','AUDIT_RQ2'),required=True,
                   help='Use once per Kubernetes resource')
    a.add_argument('--output-dir',type=Path,required=True)
    args=a.parse_args(argv)
    resources=[x[0] for x in args.dataset]
    if len(resources) != len(set(resources)):
        a.error('Each resource must be supplied exactly once')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    all_summaries=[];all_timing=[];all_case_rows=[]
    for resource,genpath,evpath,rq1path,rq2path in args.dataset:
        gen=read_csv(Path(genpath));ev=read_csv(Path(evpath));r1=read_csv(Path(rq1path));r2=read_csv(Path(rq2path))
        allowed_cases={r['case_id'] for r in ev}
        pair=[]
        for r in gen:
            if r['case_id'] not in allowed_cases or r.get('status')!='generated':
                continue
            plain=flt(r,'time_plain_manifest_total_s')
            documented=flt(r,'time_documented_manifest_total_s')
            if plain is None or documented is None:
                continue
            delta=documented-plain
            cm=flt(r,'time_build_comments_s')
            r0=flt(r,'time_render_yaml_s')
            r1time=flt(r,'time_render_documented_s')
            w0=flt(r,'time_write_yaml_s')
            w1=flt(r,'time_write_documented_s')
            rd=(r1time-r0) if r0 is not None and r1time is not None else None
            wd=(w1-w0) if w0 is not None and w1 is not None else None
            d={'resource':resource,'case_id':r['case_id'],
               'plain':plain,'documented':documented,
               'incremental':delta,'relative_pct':100*delta/plain if plain>0 else None,
               'comments':cm,'rendering_increment':rd,'writing_increment':wd}
            if all(x is not None for x in (cm,rd,wd)):
                discrepancy=delta-cm-rd-wd
                if abs(discrepancy)>0.012:
                    raise ValueError(f'Timing decomposition inconsistent for {resource}/{r["case_id"]}: {discrepancy:.6f} s')
            pair.append(d)
        all_timing.extend(pair)
        all_summaries.append(summary(resource,gen,ev,r1,r2,pair))
        for src,items in [('evaluation_metrics',ev),('audit_rq1',r1),('audit_rq2',r2)]:
            for r in items:
                all_case_rows.append({'resource':resource,'source_csv':src,**r})
    tot={k:sum(s[k] for s in all_summaries) for k in all_summaries[0]
         if k not in ('resource','timing','warnings')}
    tot['resource']='Total'
    tot['timing']={f:stats([p[f] for p in all_timing if p[f] is not None])
                   for f in ('plain','documented','incremental','comments','rendering_increment','writing_increment','relative_pct')}
    tot['warnings']=[f'{s["resource"]}: {w}' for s in all_summaries for w in s['warnings']]
    all_summaries.append(tot)
    summary_rows=[]
    for s in all_summaries:
        clean={k:v for k,v in s.items() if k not in ('timing','warnings')}
        for k,v in s['timing'].items():
            clean[f'{k}_n']=v['n'];clean[f'{k}_median_s' if k!='relative_pct' else 'relative_median_pct']=v['median']
            if k=='incremental':clean['overhead_p90_s']=v['p90']
        clean['warnings']=' | '.join(s['warnings'])
        summary_rows.append(clean)
    csv_out(args.output_dir/'rq1_summary_by_resource_and_total.csv',summary_rows)
    csv_out(args.output_dir/'rq1_paired_timings.csv',all_timing)
    csv_out(args.output_dir/'rq1_cases_merged.csv',all_case_rows)
    with (args.output_dir/'rq1_summary.json').open('w',encoding='utf-8') as f:
        json.dump(all_summaries,f,ensure_ascii=False,indent=2)
    generate_fig(all_timing,args.output_dir)
    print('\nSummary (by resource / total):')
    for s in all_summaries:
        t=s['timing']
        print(f'  {s["resource"]}: VP {s["generated_vps"]}/{s["planned_vps"]}, '
              f'manifests {s["generated_manifests"]}, schema {s["schema_passed"]}, SAT {s["sat_yaml_passed"]}, '
              f'VP agreement {s["vp_variant_reconstructed"]}/{s["vp_variant_compared"]}, '
              f'paired times {s["paired_timings"]}, median overhead {t["incremental"]["median"]} s, '
              f'P90 {t["incremental"]["p90"]} s')
    if tot['warnings']:
        print('WARNING:', *tot['warnings'],sep='\n- ')
    print('Outputs:',args.output_dir.resolve())

if __name__=='__main__':
    main()