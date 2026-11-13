#!/usr/bin/env python3
"""Paper figures and reproducible statistics for RQ1 and RQ2.

Usage (one resource):
  python plot_evaluation_rq1_rq2.py \
    --dataset Deployment PLAN.csv GENERATION.csv EVALUATION.csv \
    --defaults Deployment DEFAULT_AUDIT.csv \
    --output-dir figures

Usage (several resources):
  python plot_evaluation_rq1_rq2.py \
    --dataset Deployment D_PLAN.csv D_GEN.csv D_EVAL.csv \
    --dataset Pod P_PLAN.csv P_GEN.csv P_EVAL.csv \
    --defaults Deployment D_DEFAULTS.csv \
    --defaults Pod P_DEFAULTS.csv \
    --output-dir figures

Only generated cases belong to the evaluation denominator; UNSAT cases are
reported in the experiment flow, not included as failed YAMLs. Unknown,
not-run and non-comparable audits are never counted as failures or successes.

Timing fields are stage-derived per-output totals, *not* two independent wall
clock runs. They share the same completion stage and exclude global FM startup.

Requires: Python >= 3.9, matplotlib. No pandas or seaborn required.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean, median

import matplotlib.pyplot as plt


FMT = ('pdf', 'png')
PLOT_DPI = 220
NONCOMPARABLE = {
    '', 'not_run', 'not_evaluated', 'not_applicable', 'n/a', 'na',
    'unavailable', 'not_scored', 'no_point', 'base_or_no_point',
    'data_equal_base_not_scored', 'skipped', 'unknown', 'unverifiable',
    'not_supported', 'unsupported', 'pending', 'audit_error',
}
AUDIT_OUTCOMES = {
    'rq1_vp_fidelity_status': (
        'match', {'different_variant', 'yaml_not_exactly_one',
                  'yaml_parent_missing', 'point_not_selected_in_full',
                  'full_not_exactly_one', 'mismatch'}),
    'rq2_documentation_status': (
        'checked_match', {'comment_mismatch', 'vp_block_missing',
                          'selected_comment_missing', 'alternative_mismatch',
                          'documentation_mismatch'}),
    'rq2_vp_block_status': ('present', {'missing', 'absent', 'incorrect', 'mismatch'}),
    'rq2_selected_comment_status': (
        'correct', {'missing', 'absent', 'incorrect', 'mismatch'}),
}

METRICS_RQ1 = [
    ('YAML syntax', 'yaml_syntax_valid'),
    ('Expected GVK', 'yaml_expected_gvk_ok'),
    ('Kubeconform schema', 'schema_valid'),
    ('FM hypotheses extensible', 'fm_sat_valid'),
    ('Requested VP preserved', 'rq1_vp_fidelity_status'),
]
METRICS_RQ2 = [
    ('Plain/documented data equal', 'rq2_data_equivalent'),
    ('VP documentation correct', 'rq2_documentation_status'),
    ('VP block present', 'rq2_vp_block_status'),
    ('Selected-variant comment correct', 'rq2_selected_comment_status'),
]
DEFAULT_COLUMNS = [
    'n_default_markers_selected',
    'n_default_markers_in_alternatives',
    'n_defaultvalue_features_selected',
    'n_defaultvalue_full_equal',
    'n_defaultvalue_yaml_equal_features',
    'n_defaultvalue_yaml_equal_occurrences',
    'n_defaultvalue_yaml_different_features',
    'n_defaultvalue_inferred_applied',
    'n_defaultvalue_explicit_equal',
    'n_defaultvalue_explicit_override',
    'n_default_markers_mapped',
    'n_defaults_combined_full',
    'n_defaults_combined_observed',
]


def load_csv(path: str) -> list[dict]:
    with Path(path).open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or 'case_id' not in reader.fieldnames:
            raise ValueError(f'{path}: expected header including case_id')
        return [dict(row) for row in reader]


def index_unique(rows: list[dict], name: str) -> dict[str, dict]:
    result = {}
    for row in rows:
        case_id = (row.get('case_id') or '').strip()
        if not case_id or case_id in result:
            raise ValueError(f'{name}: missing or duplicate case_id: {case_id!r}')
        result[case_id] = row
    return result


def numeric(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def bool_value(value):
    v = str(value if value is not None else '').strip().lower()
    if v in {'true', '1', 'yes'}:
        return True
    if v in {'false', '0', 'no'}:
        return False
    return None


def status_value(value):
    return str(value if value is not None else '').strip().lower()


def audit_result(value: str, key: str):
    """Return pass, fail, or unmeasured for a known audit status.

    Unknown statuses are deliberately *not* classified as failures. Their
    frequencies remain in the diagnostic JSON for manual audit.
    """
    v = status_value(value)
    if v in NONCOMPARABLE or v.startswith('base_'):
        return None
    success, failures = AUDIT_OUTCOMES[key]
    if v == success:
        return True
    if v in failures:
        return False
    return None


def get_metric(row: dict, key: str):
    if key in AUDIT_OUTCOMES:
        return audit_result(row.get(key), key)
    # If a validator did not run or its schema was unavailable, no Boolean
    # should silently turn the case into a passed or rejected validation.
    if key == 'schema_valid' and status_value(row.get('schema_validation_status')) in {
            'unavailable', 'not_run', 'skipped', 'not_evaluated', 'no_schema'}:
        return None
    # Fields can be blank when the tool was not invoked. Do not guess.
    return bool_value(row.get(key))


def counts(values):
    result = {True: 0, False: 0, None: 0}
    for value in values:
        result[value] += 1
    return {'passed': result[True], 'failed': result[False],
            'unmeasured': result[None], 'measured': result[True] + result[False]}


def quantile(values, p):
    """Linear sample quantile, not a CI and not a benchmarking uncertainty bound."""
    if not values:
        return None
    seq = sorted(values)
    index = (len(seq) - 1) * p
    low, high = int(math.floor(index)), int(math.ceil(index))
    return seq[low] * (high - index) + seq[high] * (index - low) if low != high else seq[low]


def statistics(values):
    return {
        'n': len(values), 'mean': mean(values) if values else None,
        'median': median(values) if values else None,
        'p90': quantile(values, 0.9), 'p95': quantile(values, 0.95),
        'min': min(values) if values else None,
        'max': max(values) if values else None,
    }


def write_csv(path: Path, content: list[dict]):
    if not content:
        return
    fields = list(dict.fromkeys(key for row in content for key in row))
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(content)


def save(fig, output: Path, basename: str):
    fig.tight_layout()
    for extension in FMT:
        fig.savefig(output / f'{basename}.{extension}', dpi=PLOT_DPI,
                    bbox_inches='tight')
    plt.close(fig)


def label_value(x: float | int | None, digits=3) -> str:
    return 'NA' if x is None else f'{x:.{digits}f}'


def per_resource(name, plan_rows, gen_rows, eval_rows, defaults_rows=None):
    plan = index_unique(plan_rows, f'{name}/plan')
    generated = index_unique(gen_rows, f'{name}/generation')
    evaluated = index_unique(eval_rows, f'{name}/evaluation')
    default_by_id = index_unique(defaults_rows, f'{name}/default audit') if defaults_rows is not None else {}

    # The resource's generation record is the authority on generation status.
    outcomes = Counter()
    for case_id, plan_case in plan.items():
        gen_case = generated.get(case_id, {})
        state = status_value(gen_case.get('status'))
        if not state:
            state = 'blocked' if status_value(plan_case.get('status')) == 'blocked' else 'no_generation_record'
        outcomes[state] += 1

    genuine = [case_id for case_id, row in generated.items()
               if status_value(row.get('status')) == 'generated']
    if len(set(genuine) - set(plan)):
        print(f'Warning: {name} has generated case_ids absent from plan; they are retained.')
    ev_cases = [evaluated[case_id] for case_id in genuine
                if case_id in evaluated and status_value(evaluated[case_id].get('status')) == 'evaluated']
    evaluations = {key: counts([get_metric(row, key) for row in ev_cases])
                   for _, key in METRICS_RQ1 + METRICS_RQ2}
    metadata_status = Counter(status_value(row.get('yaml_mapping_status')) or 'unmeasured'
                              for row in ev_cases)
    all_audit_statuses = {key: dict(Counter(status_value(row.get(key)) or 'unmeasured'
                                            for row in ev_cases))
                          for _, key in METRICS_RQ1 + METRICS_RQ2 if key.startswith('rq')}
    default_label = Counter(status_value(row.get('rq2_default_explicitly_labeled')) or 'unmeasured'
                            for row in ev_cases if bool_value(row.get('rq2_selected_variant_has_default_metadata')) is True)

    expected_alternatives = 0
    verified_alternatives = 0
    auditable_alternative_cases = 0
    for row in ev_cases:
        exp, ver = numeric(row.get('rq2_alternatives_expected')), numeric(row.get('rq2_alternatives_verified'))
        if exp is not None and ver is not None:
            expected_alternatives += int(exp)
            verified_alternatives += int(ver)
            auditable_alternative_cases += 1

    paired = []
    stage_fields = ('time_build_comments_s', 'time_render_documented_s',
                    'time_render_yaml_s', 'time_write_documented_s', 'time_write_yaml_s')
    for case_id in genuine:
        item = generated[case_id]
        plain = numeric(item.get('time_plain_manifest_total_s'))
        doc = numeric(item.get('time_documented_manifest_total_s'))
        if plain is None or doc is None or plain <= 0:
            continue
        r = {'resource': name, 'case_id': case_id,
             'plain_s': plain, 'documented_s': doc,
             'extra_s': doc - plain, 'relative_extra_pct': 100.0 * (doc - plain) / plain}
        for key in stage_fields:
            r[key] = numeric(item.get(key))
        parts = [r[key] for key in stage_fields]
        if all(x is not None for x in parts):
            r['extra_comments_s'] = r['time_build_comments_s']
            r['extra_rendering_s'] = r['time_render_documented_s'] - r['time_render_yaml_s']
            r['extra_writing_s'] = r['time_write_documented_s'] - r['time_write_yaml_s']
            r['stage_delta_residual_s'] = r['extra_s'] - (
                r['extra_comments_s'] + r['extra_rendering_s'] + r['extra_writing_s'])
        else:
            r['extra_comments_s'] = r['extra_rendering_s'] = None
            r['extra_writing_s'] = r['stage_delta_residual_s'] = None
        paired.append(r)

    totals = {}
    defaults_cases = [default_by_id[c] for c in genuine if c in default_by_id]
    if defaults_rows is not None:
        totals = {key: sum(numeric(row.get(key)) or 0 for row in defaults_cases)
                  for key in DEFAULT_COLUMNS}

    summary = {
        'resource': name,
        'planned_cases': len(plan),
        'base_cases_in_plan': sum(not (row.get('point_feature') or row.get('choice_feature'))
                                  for row in plan.values()),
        'variation_point_cases_in_plan': sum(bool(row.get('point_feature') or row.get('choice_feature'))
                                             for row in plan.values()),
        'generated_cases': len(genuine),
        'unsat_cases': outcomes['unsat'],
        'other_non_generated_cases': sum(v for k, v in outcomes.items() if k not in {'generated', 'unsat'}),
        'evaluated_generated_cases': len(ev_cases),
        'no_evaluation_for_generated': len(genuine) - len(ev_cases),
        'alternatives_verified_total': verified_alternatives,
        'alternatives_expected_total': expected_alternatives,
        'alternative_audited_cases': auditable_alternative_cases,
        'mapping_full_cases': metadata_status.get('mapped', 0),
        'mapping_unmeasured_cases': metadata_status.get('unmeasured', 0),
        'timing_paired_cases': len(paired),
        'median_plain_s': statistics([r['plain_s'] for r in paired])['median'],
        'median_documented_s': statistics([r['documented_s'] for r in paired])['median'],
        'median_extra_s': statistics([r['extra_s'] for r in paired])['median'],
        'p90_extra_s': statistics([r['extra_s'] for r in paired])['p90'],
        'median_relative_extra_pct': statistics([r['relative_extra_pct'] for r in paired])['median'],
        'p90_relative_extra_pct': statistics([r['relative_extra_pct'] for r in paired])['p90'],
        'default_audit_cases': len(defaults_cases) if defaults_rows is not None else None,
        **{f'{key}_{suffix}': record[suffix]
           for key, record in evaluations.items()
           for suffix in ('passed', 'failed', 'measured', 'unmeasured')},
        **totals,
    }
    diagnostic = {
        'generation_outcomes': dict(outcomes),
        'audit_statuses': all_audit_statuses,
        'mapping_status': dict(metadata_status),
        'default_explicit_label_status_when_marker_selected': dict(default_label),
        'timing_stage_delta_residual_max_abs_s': max(
            (abs(row['stage_delta_residual_s']) for row in paired
             if row['stage_delta_residual_s'] is not None), default=None),
        'note': ('Statuses requiring interpretation are retained here. '\
                 'SAT checks extensibility of YAML mapping, not full YAML semantics. '\
                 'Timed outputs are stage-derived and share completion.'),
    }
    return {'name': name, 'outcomes': outcomes, 'summary': summary,
            'metrics': evaluations, 'timings': paired, 'diagnostic': diagnostic}


def plot_outcomes(resources, output):
    labels = [res['name'] for res in resources]
    statuses = ['generated', 'unsat', 'blocked', 'failed', 'no_generation_record']
    # Unknown outcomes are not silently dropped.
    additional = sorted({k for res in resources for k in res['outcomes']
                         if k not in statuses})
    statuses += additional
    fig, ax = plt.subplots(figsize=(7.4, max(2.3, 0.75 * len(labels) + 1.3)))
    left = [0] * len(labels)
    for status in statuses:
        values = [res['outcomes'].get(status, 0) for res in resources]
        if not any(values):
            continue
        bars = ax.barh(labels, values, left=left, label=status)
        for bar, v in zip(bars, values):
            if v:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_y() + bar.get_height()/2,
                        str(v), va='center', ha='center', fontsize=9,
                        bbox=dict(facecolor='white', edgecolor='none', alpha=.75, pad=.4))
        left = [l + v for l, v in zip(left, values)]
    ax.invert_yaxis()
    ax.set_xlabel('Planned partial configurations (including base)')
    ax.set_title('Generation outcomes — UNSAT excluded from YAML quality rates')
    ax.legend(loc='upper center', bbox_to_anchor=(.5, -.22), ncol=min(4,len(statuses)), frameon=False)
    save(fig, output, 'rq1_generation_outcomes')


def plot_success_metrics(resources, metrics, title, basename, output):
    # Separate figure, with denominators visible; absent measurements are omitted.
    unit = 0.74
    count_labels = len(resources) * len(metrics)
    fig, ax = plt.subplots(figsize=(8.8, max(2.5, unit * count_labels + .8)))
    ylabels = []
    values = []
    texts = []
    one_resource = len(resources) == 1
    for resource in resources:
        for label, key in metrics:
            item = resource['metrics'][key]
            if not item['measured']:
                continue
            ylabels.append(label if one_resource else f"{resource['name']} · {label}")
            values.append(100.0 * item['passed'] / item['measured'])
            texts.append(f"{item['passed']}/{item['measured']}  ({item['unmeasured']} unmeasured)")
    if not values:
        plt.close(fig)
        return
    bars = ax.barh(range(len(values)), values)
    for i, (bar, label) in enumerate(zip(bars, texts)):
        ax.text(bar.get_width() + 2, i, label, va='center', ha='left', fontsize=8)
    ax.set_xlim(0, 145)
    ax.set_yticks(range(len(values)), ylabels)
    ax.invert_yaxis()
    ax.set_xlabel('Passed / measured (%) — counts shown at right')
    ax.set_title(title)
    ax.grid(axis='x', alpha=.2)
    save(fig, output, basename)


def plot_output_time_distributions(resources, output):
    """Conventional time distributions; interpret together with paired deltas."""
    data, labels = [], []
    for res in resources:
        if not res['timings']:
            continue
        data.append([r['plain_s'] for r in res['timings']])
        labels.append(f"{res['name']}\nplain")
        data.append([r['documented_s'] for r in res['timings']])
        labels.append(f"{res['name']}\ndocumented")
    if not data:
        return
    fig, ax = plt.subplots(figsize=(max(6.5, .85 * len(data) + 3.6), 4.2))
    ax.boxplot(data, tick_labels=labels, showfliers=True, widths=.55)
    ax.set_ylabel('Stage-derived time (s); global FM startup excluded')
    ax.set_title('Plain vs. documented output times (shared completion stage)')
    ax.grid(axis='y', alpha=.2)
    save(fig, output, 'rq2_plain_vs_documented_times')


def plot_paired_overhead(resources, output, key, basename, ylabel):
    fig, ax = plt.subplots(figsize=(7.4, max(3.0, .65*len(resources) + 1.8)))
    groups = [[row[key] for row in res['timings']] for res in resources]
    available = [(res['name'], values) for res, values in zip(resources, groups) if values]
    if not available:
        plt.close(fig)
        return
    names, data = zip(*available)
    # Horizontal distribution of paired differences (not two independent runs).
    ax.boxplot(data, vert=False, tick_labels=names, showfliers=True, widths=.48)
    ax.axvline(0, linewidth=1, alpha=.35)
    ax.invert_yaxis()
    ax.set_xlabel(ylabel)
    ax.set_title('Incremental self-documentation cost (paired cases)')
    for i, values in enumerate(data, start=1):
        med = median(values)
        ax.text(med, i-.33, f'median={med:.3f} · n={len(values)}', fontsize=8)
    ax.grid(axis='x', alpha=.2)
    save(fig, output, basename)


def plot_incremental_components(resources, output):
    # Stage components from the SAME output pair: comments + delta rendering + delta writes.
    labels = ['Build comments', 'Extra YAML rendering', 'Extra YAML writing']
    valid = []
    for res in resources:
        pairs = [row for row in res['timings'] if row['extra_comments_s'] is not None]
        if pairs:
            valid.append((res['name'], pairs))
    if not valid:
        return
    fig, ax = plt.subplots(figsize=(7.7, max(2.6, .85 * len(valid) + 1.3)))
    for i, (name, data) in enumerate(valid):
        x = [mean([r[key] for r in data]) for key in
             ('extra_comments_s', 'extra_rendering_s', 'extra_writing_s')]
        # Positives and negatives are drawn on their own side of zero.
        positive = negative = 0.0
        for j, val in enumerate(x):
            left = positive if val >= 0 else negative
            ax.barh(i, val, left=left, label=labels[j] if i == 0 else None)
            if val >= 0:
                positive += val
            else:
                negative += val
    ax.set_yticks(range(len(valid)), [name for name, _ in valid])
    ax.invert_yaxis()
    ax.set_xlabel('Mean incremental time per case (s); signed components')
    ax.set_title('Decomposition of plain → documented cost')
    ax.axvline(0, linewidth=1, alpha=.4)
    ax.legend(loc='upper center', bbox_to_anchor=(.5, -.23), ncol=3, frameon=False)
    save(fig, output, 'rq2_incremental_cost_breakdown')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dataset', action='append', nargs=4, required=True,
                        metavar=('RESOURCE','PLAN','GENERATION','EVALUATION'))
    parser.add_argument('--defaults', action='append', nargs=2, default=[],
                        metavar=('RESOURCE','DEFAULT_AUDIT'))
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    default_files = {}
    for resource, filename in args.defaults:
        if resource in default_files:
            parser.error(f'Duplicate defaults for resource: {resource}')
        default_files[resource] = filename
    resources = []
    for name, plan_path, gen_path, ev_path in args.dataset:
        defaults = load_csv(default_files[name]) if name in default_files else None
        resource = per_resource(name, load_csv(plan_path), load_csv(gen_path),
                                load_csv(ev_path), defaults)
        resources.append(resource)

    write_csv(output / 'rq_evaluation_summary.csv', [r['summary'] for r in resources])
    write_csv(output / 'rq2_paired_timing_cases.csv',
              [item for resource in resources for item in resource['timings']])
    with (output / 'rq_audit_status_counts.json').open('w', encoding='utf-8') as handle:
        json.dump({r['name']: r['diagnostic'] for r in resources}, handle,
                  indent=2, ensure_ascii=False)
    plot_outcomes(resources, output)
    plot_success_metrics(resources, METRICS_RQ1,
                         'RQ1 · Structural correctness and decision fidelity',
                         'rq1_correctness_fidelity', output)
    plot_success_metrics(resources, METRICS_RQ2,
                         'RQ2 · Fidelity of self-documentation',
                         'rq2_documentation_fidelity', output)
    plot_output_time_distributions(resources, output)
    plot_paired_overhead(resources, output, 'extra_s', 'rq2_paired_extra_seconds',
                         'Documented minus plain stage-derived time (s)')
    plot_paired_overhead(resources, output, 'relative_extra_pct', 'rq2_paired_extra_percent',
                         '100 × (documented − plain) / plain (%)')
    plot_incremental_components(resources, output)

    print(f'Outputs: {output}')
    for r in resources:
        s = r['summary']
        print(f"{r['name']}: {s['planned_cases']} planned, {s['generated_cases']} generated, "
              f"{s['unsat_cases']} UNSAT, {s['evaluated_generated_cases']} evaluated")
        print('  RQ1 measured:', {label: f"{r['metrics'][key]['passed']}/{r['metrics'][key]['measured']}"
                                 for label, key in METRICS_RQ1})
        print('  RQ2 measured:', {label: f"{r['metrics'][key]['passed']}/{r['metrics'][key]['measured']}"
                                 for label, key in METRICS_RQ2})
        print('  Documentation overhead median (s, %):',
              label_value(s['median_extra_s']), label_value(s['median_relative_extra_pct']))
        if s['default_audit_cases'] is not None:
            print('  Default audit rows among generated:', s['default_audit_cases'])


if __name__ == '__main__':
    main()