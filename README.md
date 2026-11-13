# Self-Documenting Configuration Manifests through Default-Aware Variability Management

This repository accompanies the paper *Self-Documenting Configuration Manifests through Default-Aware Variability Management*. It contains (or, where indicated below, will contain) the implementation, Kubernetes Configuration-Aware Feature Model (CAFM), evaluation scripts, experimental results, and figures needed to inspect and reproduce the reported findings.

The approach enriches a Kubernetes Feature Model expressed in the [Universal Variability Language (UVL)](https://universal-variability-language.github.io/) with configuration keys and values, default preferences, and documentation metadata. A FlamaPy-based engine completes partial feature configurations using default-aware selection and SAT/MaxSAT reasoning, then renders ordinary and self-documenting Kubernetes YAML manifests.

## Artifact overview

| Component | Purpose |
|---|---|
| Kubernetes CAFM (UVL) | Feature hierarchy, constraints, configuration metadata and defaults |
| Manifest generator | Default-aware/minimal completion and plain/documented YAML rendering |
| YAML-to-FM mapper | Reconstruct explicit feature choices from manifests |
| RQ1 evaluation | Schema/SAT consistency, selected-variant reconstruction, comment fidelity and timing overhead |
| RQ2 evaluation | Explicit/omitted variation-point occurrences and alignment with modeled defaults in real-world and LLM-generated YAML |
| Figures and tables | Publication figures, aggregated results and supporting data |

## Reported experimental results

### RQ1 — Configuration transparency and overhead

The experiment considered **254** targeted variation points across Deployment and Pod. Of these, **242** yielded generated configurations and **12** were unsatisfiable under the tested CAFM. Including two baseline cases, **244** generated manifests (114 Deployment and 130 Pod) were evaluated.

- **244/244** passed strict Kubeconform schema validation and the YAML-to-FM SAT-extension check.
- **242/242** VP-targeted outputs yielded the selected alternative when reconstructed from YAML.
- **244/244** plain/documented manifest pairs contained equivalent YAML configuration data.
- **242/242** targeted documentation blocks and **404/404** expected alternative descriptions passed the documentation audit.
- Across **244** paired timing records, median documentation overhead was **0.554 s** (**19.1%** relative); the 90th percentile was **0.652 s**. Median times were **2.939 s** for plain and **3.530 s** for documented rendering, including shared completion costs.

### RQ2 — Real-world and LLM-generated configuration practices

The analyzed real-world subset contains **106,435** Deployment and Pod manifests (37,038 and 69,397, respectively), drawn from a larger source collection. The LLM study evaluated **229** manifests generated using **Qwen3-8B** (`qwen3:8b`) through **Ollama**, with prompts derived from FM partial configurations. Of these, **154** passed the applied schema and SAT-extension checks and were included in RQ2 variability statistics.

| Metric | Real-world Deployment | Real-world Pod | Validated LLM Deployment | Validated LLM Pod |
|---|---:|---:|---:|---:|
| Analyzed manifests | 37,038 | 69,397 | 82 | 72 |
| Explicit VP occurrences | 64,434 (21.02%) | 34,898 (5.88%) | 101 (15.98%) | 89 (17.25%) |
| Omitted VP occurrences | 242,114 (78.98%) | 558,991 (94.12%) | 531 (84.02%) | 427 (82.75%) |
| Domain-default matches | 10,276 (73.66%) | 3,007 (65.51%) | 14 (77.78%) | 15 (83.33%) |
| Numerical default matches | 34,065 (59.62%) | 1,497 (27.40%) | 101 (87.07%) | 14 (93.33%) |

**Metric interpretation.** VP explicitness and omission are computed over *structurally addressable* occurrences: an observed parent/context makes a property eligible for checking; omitted properties outside such contexts are **not** counted. A domain-default match compares a reconstructed **explicit** alternative with a unique CAFM-defined preferred variant; alternatives that only represent scalar types (`_asString`, `_asInteger`, etc.) are excluded. A numerical match compares an explicit scalar value to the CAFM `defaultValue`. Neither kind of match establishes developer intent or proves that a platform default was applied.

**Comparability and validation limits.** The real-world corpus is not filtered by schema/SAT validity in this analysis, whereas the LLM results are, and the latter are conditioned by FM-derived prompts. SAT checks establish satisfiability of a Boolean feature projection, using a representative first item per array for the SAT check; array occurrences are nevertheless counted individually for descriptive RQ2 metrics. Passing schema and SAT checks does **not** establish Kubernetes API-server admission validity. The RQ2 comparison is observational, not a controlled measurement of general LLM preferences or fidelity to a requested VP.

As a sensitivity check, after deduplicating structurally identical real-world YAML documents, domain-default alignment remained **69.38%** for Deployment and **64.05%** for Pod (versus **73.66%** and **65.51%** before deduplication). Duplicate contents are not necessarily independently sourced deployments.

## Repository layout

The following is the **target layout for the anonymous artifact**. Update file names in this section to match the files actually committed, and remove any entry not provided. Data directories may instead be distributed as separate archives when necessary.

```text
.
├── README.md
├── requirements.txt                    # Exact Python environment, if supplied
├── variability_model/
│   └── kubernetes_combined_1-37_V3_semantic.uvl
├── resources/
│   └── model_generation/
│       ├── serialization_metadata_1-37_V3.json
│       └── descriptions_1-37_V3.json
├── scripts/
│   ├── mapping_files/
│   │   └── metadata_parser01.py
│   ├── experiment_generation/
│   │   ├── <final-manifest-generator>.py
│   │   ├── evaluation_manifests_rq3.py
│   │   ├── evaluation_realworld_rq3_v3_resume.py
│   │   └── evaluation_realworld_rq3_v4_1_LLM.py
│   └── analysis/
│       ├── merge_rq1_resources_caise.py
│       └── rq3_robustness_analysis.py
├── data/
│   ├── rq1/                            # RQ1 generation/evaluation CSVs
│   ├── realworld/                      # Source YAML or documented external archive
│   └── llm/                            # LLM YAML, prompts and generation settings
├── results/
│   ├── rq1/                            # Aggregated RQ1 summary and paired timings
│   └── rq2/                            # By-resource, by-VP and observation CSVs
├── figures/
│   ├── rq1_overhead_Pod_Deploy.pdf
│   └── rq2_fig_domain_default_by_vp.pdf
└── tables/
    └── rq2_comparison.csv              # Optional, values also appear above
```

> The `<final-manifest-generator>.py` entry is a **placeholder**: replace it with the exact generator script used in the study. The layout is illustrative until the repository is populated; it is not a statement that these paths are already committed.

## Requirements and environment

The reported experiments used **Python 3.11.2** on Windows 11 (64-bit), an AMD Ryzen 7 5800X (8 cores, 3.80 GHz), and 32 GB RAM. Python dependencies include FlamaPy and its FM/SAT metamodels, PySAT, PyYAML, and the plotting dependencies. Reproduction should use the **committed `requirements.txt` or environment lock file** rather than unpinned latest package releases.

External tools:

- [Kubeconform](https://github.com/yannh/kubeconform), v0.7.0 in strict mode, using Kubernetes schema version **1.37.0** for the LLM validation command shown below.
- [Ollama](https://ollama.com/) with model tag **`qwen3:8b`** to regenerate the LLM sample. Record the resolved model digest and Ollama version alongside the prompts if exact regeneration is desired.

From the repository root:

```bash
python -m venv .venv
# Activate .venv for your platform, then:
python -m pip install -r requirements.txt
```

## Reproducing the evaluation

The commands below assume the target repository layout and that the corresponding datasets are available at the indicated paths. They use existing script interfaces; adjust paths only if the final repository uses another layout.

### RQ1 — Consolidate Deployment and Pod evaluations

The generator and per-resource evaluation should first produce, for **each** resource, `generation_times.csv`, `evaluation_metrics.csv`, `audit_rq1_vp_fidelity.csv`, and `audit_rq2_documentation.csv`. Then combine the separate measurements:

```bash
python scripts/analysis/merge_rq1_resources_caise.py \
  --dataset Deployment \
    data/rq1/Deployment/generation_times.csv \
    data/rq1/Deployment/evaluation_metrics.csv \
    data/rq1/Deployment/audit_rq1_vp_fidelity.csv \
    data/rq1/Deployment/audit_rq2_documentation.csv \
  --dataset Pod \
    data/rq1/Pod/generation_times.csv \
    data/rq1/Pod/evaluation_metrics.csv \
    data/rq1/Pod/audit_rq1_vp_fidelity.csv \
    data/rq1/Pod/audit_rq2_documentation.csv \
  --output-dir results/rq1/combined
```

Main outputs: `rq1_summary_by_resource_and_total.csv`, `rq1_paired_timings.csv`, and a combined PDF/PNG overhead figure. **The pooled median and P90 must be calculated from individual paired records; do not average the per-resource medians.** Similarly, the median of paired differences need not equal the difference of plain and documented medians.

### RQ2 — Analyze real-world manifests

```bash
python scripts/experiment_generation/evaluation_realworld_rq3_v3_resume.py \
  --source-type Real-World \
  --manifest-dir data/realworld/manifests \
  --uvl variability_model/kubernetes_combined_1-37_V3_semantic.uvl \
  --mapper scripts/mapping_files/metadata_parser01.py \
  --metadata resources/model_generation/serialization_metadata_1-37_V3.json \
  --descriptions resources/model_generation/descriptions_1-37_V3.json \
  --evaluator scripts/experiment_generation/evaluation_manifests_rq3.py \
  --output-dir results/rq2/realworld
```

This descriptive run does **not** apply schema or SAT filtering. Its outputs include `rq3_by_resource.csv`, `rq3_by_vp.csv`, `rq3_variant_frequencies.csv`, `rq3_manifests.csv`, and `rq3_variant_observations.csv`. Use `--resume` to continue an interrupted run with unchanged inputs.

### RQ2 — Analyze LLM-generated manifests

```bash
python scripts/experiment_generation/evaluation_realworld_rq3_v4_1_LLM.py \
  --source-type LLM \
  --manifest-dir data/llm/manifests \
  --uvl variability_model/kubernetes_combined_1-37_V3_semantic.uvl \
  --mapper scripts/mapping_files/metadata_parser01.py \
  --metadata resources/model_generation/serialization_metadata_1-37_V3.json \
  --descriptions resources/model_generation/descriptions_1-37_V3.json \
  --evaluator scripts/experiment_generation/evaluation_manifests_rq3.py \
  --output-dir results/rq2/llm \
  --validation both --filter-invalid \
  --kubeconform-k8s-version 1.37.0
```

Results are computed only for included documents; `rq3_manifests.csv` and `rq3_summary.json` also preserve exclusion information. In the reported run, 61/229 failed schema validation and 14 additional manifests failed the selected SAT projection. A separate metadata link between each prompt and its requested VP would be needed to measure **prompt-target fidelity**; the RQ2 summary does not measure that outcome.

### Optional: real-world deduplication/default sensitivity

The `rq3_robustness_analysis.py` postprocessor combines aggregated and per-document RQ2 CSVs to compare the original corpus with one representative per normalized YAML content hash. It also separates type-representation alternatives from domain alternatives. See the script's `--help` for input paths and options. Do not mix pilot per-document CSVs with full-corpus aggregate files.

## Figures, tables, and provenance

The `figures/` directory should include the **vector PDFs** used in the paper (and optionally PNG previews). The `tables/` or `results/` directory should retain the machine-readable numbers used to generate each table, along with scripts or captions documenting the grouping and denominators.

## References and tools

- [FlamaPy](https://www.flamapy.org/)
- [Universal Variability Language](https://universal-variability-language.github.io/)
- [Kubernetes](https://kubernetes.io/docs/)
- [Kubeconform](https://github.com/yannh/kubeconform)
- [Ollama](https://ollama.com/)