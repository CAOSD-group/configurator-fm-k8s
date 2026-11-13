#!/usr/bin/env python3
"""Recreate the original CAiSE RQ1 overhead figure from combined paired timings.

Reads rq1_paired_timings.csv (one row per manifest, with resource, case_id,
plain, documented, incremental, relative_pct, comments, rendering_increment,
writing_increment). Keeps the visual grammar of the Deployment-only figure:
zero dashed reference, visible scatter, thick colored IQR and black median ticks,
with a two-line median/P90 annotation at the right.

Usage:
  python plot_rq1_overhead_estilo_original.py \
    --timings rq1_paired_timings.csv --output-dir ./figures
"""
from __future__ import annotations
import argparse
import csv
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator, FormatStrFormatter

ROWS = [
    ('incremental', 'Total documentation overhead', '#38698b'),
    ('comments', 'Comment generation', '#e5964d'),
    ('rendering_increment', 'Additional YAML rendering', '#70aa93'),
]


def load_pairs(path: Path):
    with path.open('r', encoding='utf-8-sig', newline='') as fp:
        pairs = list(csv.DictReader(fp))
    if not pairs:
        raise ValueError('Empty paired timings CSV')
    required = {key for key, _, _ in ROWS}
    if not required.issubset(pairs[0]):
        raise ValueError('Expected columns missing: ' + ', '.join(sorted(required - set(pairs[0]))))
    values = {}
    for key, _, _ in ROWS:
        values[key] = np.asarray([float(row[key]) for row in pairs if row.get(key) not in (None, '')], dtype=float)
        if len(values[key]) != len(pairs):
            raise ValueError(f'{key} must contain one time per paired manifest; found {len(values[key])}/{len(pairs)}')
    return pairs, values


def make_figure(pairs, values, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    n = len(pairs)
    fig, ax = plt.subplots(figsize=(8.45, 3.30))
    # Leave a narrow white region at right for readable, non-overlapping stats.
    fig.subplots_adjust(left=.315, right=.805, bottom=.235, top=.905)
    rng = np.random.default_rng(1137)

    ax.set_axisbelow(True)
    ax.grid(axis='x', color='#d9dfe5', linewidth=.75)
    ax.axvline(0, color='#788e9e', ls='--', linewidth=1.25, alpha=.9, zorder=4)
    statistics = {}

    for i, (key, label, color) in enumerate(ROWS):
        arr = values[key]
        median = float(np.median(arr))
        q25, q75 = np.percentile(arr, [25, 75])
        p90 = float(np.percentile(arr, 90))
        # Slightly denser than the old figure because there are now 244 cases.
        jitter = rng.uniform(-.115, .115, len(arr))
        ax.scatter(arr, i + jitter, color=color, alpha=.58, s=13,
                   linewidths=0, rasterized=True, zorder=3)
        # Important: keep the originally visible thick IQR and median marker.
        ax.hlines(i, q25, q75, colors=color, linewidth=9,
                  alpha=.91, zorder=5)
        ax.vlines(median, i-.175, i+.175, color='#1e252d',
                  linewidth=2.35, zorder=6)
        statistics[key] = (median, p90)

    ax.set_yticks(range(len(ROWS)), [label for _, label, _ in ROWS])
    ax.invert_yaxis()
    ax.set_ylim(len(ROWS)-.54, -.53)
    ax.set_xlim(-.032, 1.035)
    ax.xaxis.set_major_locator(MultipleLocator(.2))
    ax.xaxis.set_major_formatter(FormatStrFormatter('%.1f'))
    ax.set_xlabel('Additional time per manifest (s)', fontsize=10.5)
    ax.tick_params(axis='x', labelsize=9, color='#4e5863', length=4)
    ax.tick_params(axis='y', labelsize=10.4, length=0, pad=11)
    for sp in ('top', 'right', 'left'):
        ax.spines[sp].set_visible(False)
    ax.spines['bottom'].set_color('#84909a')

    med, p90 = statistics['incremental']
    fig.text(.83, .817, f'Median {med:.3f} s\nP90 {p90:.3f} s',
             fontsize=10.2, ha='left', va='center', color='#1b2730', linespacing=1.24)

    stem = 'rq1_overhead_combined_estilo_original'
    fig.savefig(output_dir / f'{stem}.pdf', bbox_inches='tight', facecolor='white')
    fig.savefig(output_dir / f'{stem}.png', dpi=230, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f'Cases: {n}; overhead median: {med:.6f} s; P90: {p90:.6f} s')
    print('Saved:', output_dir / (stem + '.pdf'))
    print('Saved:', output_dir / (stem + '.png'))


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--timings', type=Path, required=True)
    cli.add_argument('--output-dir', type=Path, default=Path('.'))
    args = cli.parse_args()
    pairs, values = load_pairs(args.timings)
    make_figure(pairs, values, args.output_dir)


if __name__ == '__main__':
    main()