### Deployment

| Metric | Value |
|---|---:|
| **Experimental setup** | |
| Planned partial configurations | 120 |
| Generated / evaluated manifests | 114 / 114 |
| Unsatisfiable inputs (not rendered) | 6 |
| **RQ1 · Correctness and fidelity** | |
| YAML syntax valid | 114/114 |
| Expected GVK | 114/114 |
| Kubeconform schema validity | 114/114 |
| SAT extensibility of mapped hypotheses | 114/114 |
| Variation-point selection fidelity | 113/113 |
| **RQ2 · Transparency and cost** | |
| Plain/documented data equivalence | 114/114 |
| Correct VP documentation | 113/113 |
| Alternative descriptions verified | 195/195 |
| Selected default-marked variants | 39 |
| Selected default markers mapped to YAML | 34 |
| Inferred scalar defaults observed in YAML | 15 |
| Plain / documented median times (s) | 2.863 / 3.422 |
| Paired median extra time (s) | 0.527 |
| Paired median relative overhead (%) | 18.56 |
| Paired P90 extra time (s) | 0.592 |
