#!/usr/bin/env python3
"""Publication-oriented RQ1/RQ2 tables and RQ2 figures from the existing evaluation summaries.

This is a POST-PROCESSING companion to plot_rq1_rq2.py. First produce
rq_evaluation_summary.csv and rq2_paired_timing_cases.csv with the original
pipeline, then run:

    python plot_caise_results_v2.py \
      --summary figuresRQs/rq_evaluation_summary.csv \
      --paired figuresRQs/rq2_paired_timing_cases.csv \
      --evaluation Deployment path/to/evaluation_metrics.csv \
      --output-dir figuresRQs/paper

Handles multiple resources automatically when more --dataset entries are
processed by the original script. Requires matplotlib only (standard-library
CSV, statistics, math and random; no pandas or seaborn).

Timing caveat: values are sums of measured pipeline stages, share SAT/MaxSAT
completion, and exclude global FM start-up. Do not interpret paired times as
independent benchmark executions. Summaries are not hypothesis tests.
"""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from collections import defaultdict
from statistics import mean, median
import random
import shutil

import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

PALETTE = ['#275D89', '#E18B44', '#4F8A7C', '#8A669E', '#AF7353', '#607A95']
GRID = '#E2E6E9'
INK = '#243240'


def rows(path):
    with Path(path).open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def number(value, default=None):
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (ValueError, TypeError):
        return default


def count(value):
    n = number(value)
    return int(round(n)) if n is not None else None


def fmt_count(v):
    n = count(v)
    return str(n) if n is not None else 'n/a'


def fnum(n, digits=3):
    return f'{n:.{digits}f}' if n is not None else 'n/a'


def quantile(seq, p):
    a = sorted(seq)
    if not a:
        return None
    x = (len(a) - 1) * p
    i, j = math.floor(x), math.ceil(x)
    return a[i] + (a[j] - a[i]) * (x - i)


def clean_style():
    plt.rcParams.update({
        'font.family': 'DejaVu Sans',
        'font.size': 9,
        'axes.labelsize': 9,
        'axes.titlesize': 11,
        'xtick.labelsize': 8,
        'ytick.labelsize': 9,
        'axes.spines.top': False,
        'axes.spines.right': False,
        'axes.edgecolor': '#98A4AF',
        'axes.labelcolor': INK,
        'text.color': INK,
        'xtick.color': INK,
        'ytick.color': INK,
        'pdf.fonttype': 42,
        'ps.fonttype': 42,
        'savefig.facecolor': 'white',
    })


def save(fig, directory, stem):
    fig.savefig(directory / (stem + '.pdf'), bbox_inches='tight')
    fig.savefig(directory / (stem + '.png'), dpi=230, bbox_inches='tight')
    plt.close(fig)


def get_metric(s, name):
    passed = count(s.get(name + '_passed')) or 0
    measured = count(s.get(name + '_measured')) or 0
    unmeasured = count(s.get(name + '_unmeasured')) or 0
    return (passed, measured, unmeasured)


def ratio(s, name):
    p, m, u = get_metric(s, name)
    return f'{p}/{m}' if m else 'n/a'


def path_slug(v):
    return ''.join(c.lower() if c.isalnum() else '_' for c in v).strip('_') or 'resource'


def table_markdown(s):
    n = s['resource']
    scenarios = [
      ('Experimental setup', None),
      ('Planned partial configurations', f"{fmt_count(s.get('planned_cases'))}"),
      ('Generated / evaluated manifests', f"{fmt_count(s.get('generated_cases'))} / {fmt_count(s.get('evaluated_generated_cases'))}"),
      ('Unsatisfiable inputs (not rendered)', fmt_count(s.get('unsat_cases'))),
      ('RQ1 · Correctness and fidelity', None),
      ('YAML syntax valid', ratio(s, 'yaml_syntax_valid')),
      ('Expected GVK', ratio(s, 'yaml_expected_gvk_ok')),
      ('Kubeconform schema validity', ratio(s, 'schema_valid')),
      ('SAT extensibility of mapped hypotheses', ratio(s, 'fm_sat_valid')),
      ('Variation-point selection fidelity', ratio(s, 'rq1_vp_fidelity_status')),
      ('RQ2 · Transparency and cost', None),
      ('Plain/documented data equivalence', ratio(s, 'rq2_data_equivalent')),
      ('Correct VP documentation', ratio(s, 'rq2_documentation_status')),
      ('Alternative descriptions verified', f"{fmt_count(s.get('alternatives_verified_total'))}/{fmt_count(s.get('alternatives_expected_total'))}"),
      ('Selected default-marked variants', fmt_count(s.get('n_default_markers_selected'))),
      ('Selected default markers mapped to YAML', fmt_count(s.get('n_default_markers_mapped'))),
      ('Inferred scalar defaults observed in YAML', fmt_count(s.get('n_defaultvalue_inferred_applied'))),
      ('Plain / documented median times (s)', f"{fnum(number(s.get('median_plain_s')))} / {fnum(number(s.get('median_documented_s')))}"),
      ('Paired median extra time (s)', fnum(number(s.get('median_extra_s')))),
      ('Paired median relative overhead (%)', fnum(number(s.get('median_relative_extra_pct')), 2)),
      ('Paired P90 extra time (s)', fnum(number(s.get('p90_extra_s')))),
    ]
    text = [f'### {n}', '', '| Metric | Value |', '|---|---:|']
    for label, val in scenarios:
        if val is None:
            text.append(f'| **{label}** | |')
        else:
            text.append(f'| {label} | {val} |')
    return '\n'.join(text)


def texsafe(s):
    return (str(s).replace('\\', r'\textbackslash{}').replace('_', r'\_')
            .replace('%', r'\%').replace('&', r'\&').replace('#', r'\#'))


def table_tex(s):
    # Designed for a single-column LNCS layout (compact, \\small, no giant bars).
    n = s['resource']
    secs = [
        ('Experimental setup', [
            ('Partial inputs / generated / UNSAT',
             f"{fmt_count(s.get('planned_cases'))} / {fmt_count(s.get('generated_cases'))} / {fmt_count(s.get('unsat_cases'))}")]),
        ('RQ1: Correctness and Fidelity', [
            ('YAML / GVK / schema validity', ' / '.join(ratio(s, x) for x in
                ('yaml_syntax_valid', 'yaml_expected_gvk_ok', 'schema_valid'))),
            ('Mapped-hypothesis SAT extensibility', ratio(s, 'fm_sat_valid')),
            ('Requested VP selection preserved', ratio(s, 'rq1_vp_fidelity_status')),
        ]),
        ('RQ2: Transparency and Cost', [
            ('Plain/documented data equivalent', ratio(s, 'rq2_data_equivalent')),
            ('Variation-point comments correct', ratio(s, 'rq2_documentation_status')),
            ('Documented alternatives verified', f"{fmt_count(s.get('alternatives_verified_total'))}/{fmt_count(s.get('alternatives_expected_total'))}"),
            ('Default markers selected / YAML-mapped',
             f"{fmt_count(s.get('n_default_markers_selected'))} / {fmt_count(s.get('n_default_markers_mapped'))}"),
            ('Scalar default values inferred in YAML', fmt_count(s.get('n_defaultvalue_inferred_applied'))),
            ('Median time: plain / documented (s)',
             f"{fnum(number(s.get('median_plain_s')))} / {fnum(number(s.get('median_documented_s')))}"),
            ('Paired overhead: median / P90 (s)',
             f"{fnum(number(s.get('median_extra_s')))} / {fnum(number(s.get('p90_extra_s')))}"),
            ('Paired relative overhead: median (\\%)',
             fnum(number(s.get('median_relative_extra_pct')), 2)),
        ]),
    ]
    lines = [r'% Requires \usepackage{booktabs}',
             r'\begin{table}[t]', r'\centering', r'\small',
             rf'\caption{{Evaluation summary for {texsafe(n)}. Rates use measured-case denominators.}}',
             rf'\label{{tab:eval-{path_slug(n)}}}',
             r'\begin{tabular}{@{}lr@{}}', r'\toprule',
             r'\textbf{Metric} & \textbf{Value} \\', r'\midrule']
    for idx, (heading, items) in enumerate(secs):
        if idx:
            lines.append(r'\addlinespace[2pt]')
        lines.append(rf'\multicolumn{{2}}{{@{{}}l}}{{\textit{{{texsafe(heading)}}}}} \\')
        for label, v in items:
            # \% in a label explicitly preserved.
            label_escaped = texsafe(label).replace(r'\textbackslash{}\textbackslash{}%', r'\%')
            if '(\\%)' in label:
                label_escaped = texsafe(label.replace(' (\\%)', '')) + r' (\%)'
            lines.append(f'{label_escaped} & {texsafe(v)} ' + r'\\')
    lines += [r'\bottomrule', r'\end{tabular}',
              r'\par\vspace{1mm}',
              r'{\footnotesize The base case is not applicable to VP-specific audits. '
              r'SAT results concern extensibility of mapped hypotheses; '
              r'times exclude global FM initialization and share completion.\par}',
              r'\end{table}']
    return '\n'.join(lines) + '\n'


def plot_paired_scatter(resource, pair, directory):
    if not pair:
        return
    plain = [r['plain_s'] for r in pair]
    doc = [r['documented_s'] for r in pair]
    deltas = [r['extra_s'] for r in pair]
    delta = median(deltas)
    fig, ax = plt.subplots(figsize=(6.15, 4.45), layout='constrained')
    xmin, xmax = min(plain), max(plain)
    ymin, ymax = min(doc), max(doc)
    left = min(xmin, ymin) - .15
    right = max(xmax, ymax) + .17
    ref_x = [left, right]
    ax.plot(ref_x, ref_x, '--', color='#9AA6B0', linewidth=1.25, label='No extra cost')
    ax.plot(ref_x, [t+delta for t in ref_x], '-', color=PALETTE[1],
            linewidth=1.5, alpha=.9, label=f'Median shift (+{delta:.3f} s)')
    ax.scatter(plain, doc, marker='o', s=15, edgecolors='none', color=PALETTE[0], alpha=.78,
               label=f'{resource} (n={len(pair)})', zorder=3)
    ax.set_xlim(xmin-.18, xmax+.2)
    ax.set_ylim(ymin-.22, ymax+.17)
    ax.set_xlabel('Plain output time (s)')
    ax.set_ylabel('Documented output time (s)')
    ax.set_title('Paired output costs')
    ax.grid(color=GRID, linewidth=.75, alpha=.8)
    ax.legend(frameon=False, loc='upper left', fontsize=8)
    ax.text(.99, .03, 'Shared completion; global FM startup excluded',
            transform=ax.transAxes, ha='right', fontsize=7.7, color='#65727B')
    save(fig, directory, f'rq2_paired_scatter_{path_slug(resource)}')


def plot_component_dots(resource, pair, directory):
    """Compact LNCS-ready distribution of RQ2 documentation overhead.

    - No title or in-figure legend: the LaTeX caption provides the context.
    - Only the *total* overhead is annotated (median and P90).
    - Small solid dots show individual cases; a thick segment shows the IQR;
      a vertical tick shows the median. Reproducible jitter avoids overlap.
    - The writing-time difference is deliberately omitted from the plot:
      it is negligible and can be reported in the paper text.
    """
    selected = [
        ('Total documentation overhead', 'extra_s', PALETTE[0]),
        ('Comment generation', 'extra_comments_s', PALETTE[1]),
        ('Additional YAML rendering', 'extra_rendering_s', PALETTE[2]),
    ]
    present = [(label, key, color, [r[key] for r in pair if r.get(key) is not None])
               for label, key, color in selected]
    present = [entry for entry in present if entry[3]]
    if not present:
        return

    # Wide enough for labels, compact enough for a single LNCS column.
    fig, ax = plt.subplots(figsize=(6.1, 2.65), layout='constrained')
    rng = random.Random(2027)
    ypos = list(range(len(present) - 1, -1, -1))
    values = [v for _, _, _, nums in present for v in nums]
    data_max = max(values)
    data_min = min(values)
    left = min(-0.025, data_min - .02)
    # Room for the two total-overhead statistics on the right.
    right = data_max + .18

    for y, (label, field, color, nums) in zip(ypos, present):
        jittered_y = [y + rng.uniform(-.15, .15) for _ in nums]
        ax.scatter(nums, jittered_y, marker='o', s=12.5, edgecolors='none',
                   alpha=.68, color=color, zorder=3)
        q1, q3 = quantile(nums, .25), quantile(nums, .75)
        med = median(nums)
        ax.hlines(y, q1, q3, color=INK, lw=5, zorder=4, alpha=.88)
        ax.vlines(med, y-.23, y+.23, color=INK, lw=2.3, zorder=5)

        # The component medians and P90s are not repeated: already described
        # in the RQ2 text; show only the overall cost next to the top row.
        if field == 'extra_s':
            p90 = quantile(nums, .9)
            annotation_x = data_max + .035
            ax.text(annotation_x, y+.10, f'Median {med:.3f} s',
                    ha='left', va='center', fontsize=9.3, color=INK)
            ax.text(annotation_x, y-.10, f'P90 {p90:.3f} s',
                    ha='left', va='center', fontsize=9.0, color='#64717B')

    ax.axvline(0, ls='--', color='#9AA6B0', lw=1.0, zorder=1)
    ax.set_xlim(left, right)
    ax.set_ylim(-.47, max(ypos) + .47)
    ax.set_yticks(ypos, [x[0] for x in present])
    ax.set_xlabel('Additional time per manifest (s)', fontsize=10)
    ax.tick_params(axis='x', labelsize=9)
    ax.tick_params(axis='y', labelsize=10, length=0, pad=7)
    ax.grid(axis='x', color=GRID, linewidth=.75)
    ax.set_axisbelow(True)
    # No in-figure title or footer: keep the figure uncluttered.
    save(fig, directory, f'rq2_component_dots_{path_slug(resource)}')


def plot_mean_cost_decomposition(by_resource, directory):
    # A *signed* stage decomposition. Writing differences may be negative
    # due to small timing noise and must not be clipped or silently omitted.
    candidates = []
    for name, items in by_resource.items():
        good = [r for r in items if all(r.get(k) is not None for k in
                ('extra_comments_s','extra_rendering_s','extra_writing_s'))]
        if good:
            candidates.append((name, good))
    if not candidates:
        return
    fig, ax = plt.subplots(figsize=(6.9, max(2.6, .61*len(candidates)+1.6)), layout='constrained')
    labels = [('Comments', 'extra_comments_s', PALETTE[0]),
              ('Additional rendering', 'extra_rendering_s', PALETTE[1]),
              ('Writing delta', 'extra_writing_s', PALETTE[2])]
    for i, (name, items) in enumerate(candidates):
        pos = neg = 0.0
        for label, field, col in labels:
            value = mean([r[field] for r in items])
            left = pos if value >= 0 else neg
            ax.barh(i, value, left=left, height=.46, color=col,
                    label=label if i==0 else None)
            if abs(value) > .035:
                ax.text(left+value/2, i, f'{value:.3f}', ha='center', va='center',
                        fontsize=9, color='white', fontweight='bold')
            if value >= 0:
                pos += value
            else:
                neg += value
        total = mean([r['extra_s'] for r in items])
        ax.text(max(pos, total)+.012, i, f'{total:.3f} s total', va='center',
                ha='left', fontsize=8.5)
    ax.set_yticks(range(len(candidates)), [x[0] for x in candidates])
    ax.invert_yaxis()
    ax.set_xlabel('Mean incremental time per case (s)')
    ax.set_title('Incremental cost: mean contribution of each stage')
    ax.grid(axis='x', color=GRID)
    ax.axvline(0, lw=.9, color=INK, alpha=.5)
    ax.set_xlim(left=min(-.03, ax.get_xlim()[0]), right=ax.get_xlim()[1]+.14)
    ax.legend(frameon=False, loc='upper center', bbox_to_anchor=(.50,-.28), ncol=3, fontsize=8)
    save(fig, directory, 'rq2_mean_cost_decomposition')


def plot_overhead_overview(by_resource, directory):
    # Multi-resource comparison (adds value once there are >=2 resources).
    if len(by_resource) < 2:
        return
    fig, ax = plt.subplots(figsize=(7, max(2.8, .64*len(by_resource)+1.4)), layout='constrained')
    rng = random.Random(2027)
    for i, (name, pair) in enumerate(by_resource.items()):
        a = [r['extra_s'] for r in pair]
        if not a: continue
        ax.scatter(a, [i+rng.uniform(-.13,.13) for _ in a], marker='o',
                   color=PALETTE[i%len(PALETTE)], s=11, edgecolors='none', alpha=.65)
        ax.hlines(i, quantile(a,.25), quantile(a,.75), lw=6, color=INK)
        ax.vlines(median(a), i-.23, i+.23, lw=2, color=INK)
        ax.text(max(a)+.03, i, f'{median(a):.3f}s · n={len(a)}', va='center',fontsize=8)
    ax.set_yticks(range(len(by_resource)), list(by_resource))
    ax.invert_yaxis()
    ax.grid(axis='x',color=GRID)
    ax.set_xlabel('Additional self-documentation time per case (s)')
    ax.set_title('Paired cost by resource')
    save(fig, directory, 'rq2_overhead_across_resources')



def plot_overhead_vs_alternatives(resource, pair, evaluations, directory):
    """Optional exploratory chart: timing overhead vs verified alternative descriptions.

    A count is used only when the self-documentation audit verified the number
    of alternatives. No unverified/missing count is filled in with zero.
    """
    by_id={}
    for record in evaluations:
        cid=record.get('case_id','').strip()
        if not cid or cid in by_id:
            continue
        if record.get('rq2_documentation_status','').strip().lower() != 'checked_match':
            continue
        n=number(record.get('rq2_alternatives_verified'))
        expected=number(record.get('rq2_alternatives_expected'))
        if n is not None and expected is not None and n==expected and n>=0 and n.is_integer():
            by_id[cid]=int(n)
    points=[(by_id[r['case_id']],r['extra_s']) for r in pair if r.get('case_id') in by_id]
    if len(points)<5:
        return
    groups=defaultdict(list)
    for n,delta in points:
        groups[n].append(delta)
    rng=random.Random(2027)
    fig,ax=plt.subplots(figsize=(6.5,3.7),layout='constrained')
    for n, vals in sorted(groups.items()):
        xx=[n+rng.uniform(-.105,.105) for _ in vals]
        ax.scatter(xx,vals, marker='o',edgecolors='none',s=13,
                   color=PALETTE[0],alpha=.63)
    shown=[(n,median(vals)) for n,vals in sorted(groups.items()) if len(vals)>=3]
    if len(shown)>1:
        ax.plot([p[0] for p in shown],[p[1] for p in shown],color=PALETTE[1],
                marker='D', markersize=4,linewidth=1.4,label='Median (groups with n ≥ 3)')
        ax.legend(frameon=False,fontsize=8)
    ax.set_xlim(min(groups)-.35,max(groups)+.35)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel('Verified alternative descriptions per manifest')
    ax.set_ylabel('Additional documentation time (s)')
    ax.set_title(f'Documentation cost vs. alternatives · {resource}')
    ax.grid(axis='y',color=GRID)
    fig.text(.98, .005, 'Exploratory association; not a causal estimate.',
             fontsize=7.5,ha='right',color='#65727B')
    save(fig,directory,f'rq2_cost_vs_alternatives_{path_slug(resource)}')

def main():
    p=argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--summary', required=True, type=Path)
    p.add_argument('--paired', required=True, type=Path)
    p.add_argument('--evaluation', action='append', nargs=2, default=[],
                   metavar=('RESOURCE', 'EVALUATION_CSV'),
                   help='Optional source CSV for plotting cost vs documented alternatives')
    p.add_argument('--output-dir', required=True, type=Path)
    args=p.parse_args()
    target = args.output_dir
    target.mkdir(parents=True,exist_ok=True)
    summaries=rows(args.summary)
    raw_pairs=rows(args.paired)
    evaluation_sources=dict(args.evaluation)
    if not summaries:
        p.error('summary CSV has no rows')
    if not all('resource' in s for s in summaries):
        p.error('summary CSV lacks resource column')
    names=[s['resource'] for s in summaries]
    if len(names)!=len(set(names)):
        p.error('resource names must be unique')
    grouped=defaultdict(list)
    for row in raw_pairs:
        name=row.get('resource')
        if not name or name not in names:
            p.error(f'unknown resource in paired CSV: {name!r}')
        must=('plain_s','documented_s','extra_s','relative_extra_pct')
        if any(number(row.get(x)) is None for x in must):
            p.error(f'invalid timing row: {row.get("case_id")!r}')
        record={'case_id':row.get('case_id')}
        for key in must + ('extra_comments_s','extra_rendering_s','extra_writing_s'):
            record[key]=number(row.get(key))
        if not math.isclose(record['documented_s']-record['plain_s'],record['extra_s'],rel_tol=1e-8,abs_tol=1e-6):
            p.error(f'inconsistent paired difference: {record["case_id"]}')
        grouped[name].append(record)
    clean_style()
    for s in summaries:
        name=s['resource']
        if count(s.get('timing_paired_cases')) is not None and count(s.get('timing_paired_cases'))!=len(grouped[name]):
            p.error(f'{name}: paired row count does not match summary')
        (target/f'rq_table_{path_slug(name)}.tex').write_text(table_tex(s),encoding='utf-8')
        plot_paired_scatter(name,grouped[name],target)
        plot_component_dots(name,grouped[name],target)
        if name in evaluation_sources:
            plot_overhead_vs_alternatives(name,grouped[name],rows(evaluation_sources[name]),target)
    (target/'rq_tables.md').write_text('\n\n'.join(table_markdown(s) for s in summaries)+'\n',encoding='utf-8')
    plot_mean_cost_decomposition(grouped,target)
    plot_overhead_overview(grouped,target)
    # Convenience name matching the LaTeX includegraphics path used in the paper.
    # Only when a single resource is plotted, so no file is accidentally overwritten.
    if len(summaries) == 1:
        source_stem = f'rq2_component_dots_{path_slug(summaries[0]["resource"])}'
        for extension in ('.pdf', '.png'):
            source = target / (source_stem + extension)
            if source.exists():
                shutil.copyfile(source, target / ('rq2_timings' + extension))
    print(f'Created paper figures and tables in: {target}')
    for s in summaries:
        name=s['resource']
        a=[r['extra_s'] for r in grouped[name]]
        print(f'{name}: {len(a)} paired cases; median Δ={median(a):.3f}s; P90 Δ={quantile(a,.90):.3f}s; '
              f'comments mean={mean(r["extra_comments_s"] for r in grouped[name] if r["extra_comments_s"] is not None):.3f}s')

if __name__=='__main__':
    main()