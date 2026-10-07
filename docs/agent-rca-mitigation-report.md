# Agent-based RCA & Mitigation on SREGym — Progress Report

**Scope:** GPT-5 (OpenAI) running via the `kagent` framework, across 5 live Kubernetes clusters
(`sre-test1`, `sre-test2`, `workload01`, `workload02`, `workload03`).
**Data window:** 2026-08-18 → 2026-08-21.
**Valid runs analyzed:** 186 (deployed & completed, deduplicated).
**Problem coverage:** 18 / 117 registered `problem_id`s (15.4% of the catalog).

**Data source:** `traces.db` pulled directly off the PVC of all 5 `sregym-runner-*` pods
(namespace `sregym-runner`, path `/workspace/results/`), merged with every per-batch
`*_ALL_results.csv` found under each pod's results tree, then deduplicated on
`(cluster, problem_id, attempt, TTL, TTM, steps)`. This is the *live* dataset, not the stale
host-side `results/traces.db` in this repo (only 26 rows, frozen at 2026-08-06).

---

## Headline numbers

| Metric | Value |
|---|---|
| Valid runs (deployed & completed) | 186 |
| Diagnosis success rate | 56.0% (98/175) |
| Mitigation success rate | 43.8% (63/144) |
| Clean trajectories for SFT (`mitigation_success = True`) | 63 |
| Average tool calls per run | 16.5 |
| Model / framework | `gpt-5` (OpenAI) via `kagent` |

11 of 186 runs never reached a diagnosis submission (timeout/error before `submit`); a further
31 reached diagnosis but have no recorded mitigation outcome — hence the shrinking denominators
(186 → 175 → 144).

---

## A. Agent-based RCA & Mitigation results

### A.1 — Accuracy by fault family

"Diagnosis success" = the agent's root-cause text passes the LLM-judge checklist against ground
truth. "Mitigation success" = the state-probe oracle confirms the cluster is actually healthy
again.

| Fault family | # problem_ids | n runs | Diagnosis success | Mitigation success | Avg. steps/run |
|---|---|---|---|---|---|
| DNS / Storage / Scheduling / Resource | 7 | 44 | **93.0%** | 41.0% | 15.3 |
| Config / Image (repeated baseline set) | 6 | 127 | 47.0% | 48.9% | 17.6 |
| Network / Security / TLS | 5 | 15 | **20.0%** | **20.0%** | **29.7** |

**Key takeaway:** the Network/Security/TLS family is the worst performer on every axis —
lowest diagnosis and mitigation accuracy, yet the *highest* step count per run. The agent spends
the most effort exactly where it is least effective — a negative-ROI pattern worth addressing
before scaling this family's coverage further.

### A.2 — Where diagnosis breaks down (3-dimension scoring)

Average score per LLM-judge dimension, n=175 scored runs:

| Dimension | Meaning | Avg. score |
|---|---|---|
| D1 — Fault Localization | Correctly identifies the failing component | 69.5% |
| D2 — Fault Characterization | Correctly explains the technical mechanism/cause | **52.4% (weakest)** |
| D3 — Scope Precision | Doesn't over-attribute blame to unrelated components | 59.5% |

The agent is noticeably better at pointing to *where* the fault is (D1) than at correctly
explaining *what* is technically wrong with it (D2). The bottleneck in diagnosis accuracy is
characterization, not localization.

### A.3 — Diagnosis is (almost) a precondition for mitigation

| Condition | n | Mitigation success |
|---|---|---|
| Diagnosis correct | 82 | **70.7%** (58/82) |
| Diagnosis incorrect | 62 | **8.1%** (5/62) |

A correct diagnosis increases mitigation success probability by **~8.8x**. This validates
SREGym's diagnosis-gated benchmark design — with two notable exceptions, discussed in A.6.

### A.4 — Operational metrics: tool calls, timing, cost

**Tool call breakdown (3,054 total calls, 185 runs with a recorded breakdown):**

| Tool | Total calls | Share |
|---|---|---|
| `exec_kubectl_cmd_safely` | 2,666 | 87.3% |
| `submit` | 235 | 7.7% |
| `get_alerts` (Prometheus) | 84 | 2.8% |
| `get_traces` (Jaeger) | 42 | 1.4% |
| `get_metrics` (Prometheus) | 20 | 0.7% |
| `get_dependency_graph` / `get_services` (Jaeger) | 6 | 0.2% |
| `rollback_command` / `get_previous_rollbackable_cmd` | 1 | 0.03% |

kubectl accounts for 87% of all tool calls. Prometheus + Jaeger combined account for only 5%,
despite both being available to the agent. `rollback_command` is essentially never used — the
agent almost never backs out of a bad mitigation attempt; it just tries a new fix forward.

**Steps / timing / tokens (n=185 unless noted):**

| Metric | Median | Mean | Max |
|---|---|---|---|
| Steps per run | 15 | 18.0 | 70 |
| Tool calls per run | 13 | 16.5 | 68 |
| TTL — time-to-localize (s) | 74.7 | 119.4 | 902.2 |
| TTM — time-to-mitigate (s) | 370.7 | 373.5 | 1,134.2 |
| Prompt tokens per run (n=166) | 202,498 | 328,211 | 1,936,955 |

**Data gap:** `Metrics.total_cost_usd` is `null` in 100% of the 186 runs, even though
prompt/completion tokens are populated in 89% of them. This pipeline gap needs to be fixed
before $/trajectory or $/successful-mitigation can be reported.

### A.5 — Fast when right, slow when wrong

| | n | Median TTL (s) |
|---|---|---|
| Diagnosis correct | 98 | **58.3** |
| Diagnosis incorrect | 77 | **122.7** |

When the agent's first hypothesis is right, it converges in about a minute. When it's wrong, it
typically keeps investigating longer (2.1x the median time) — but that extra time rarely rescues
the wrong initial hypothesis. This is consistent with A.2: the bottleneck is evidence
interpretation, not investigation time.

### A.6 — Notable qualitative findings

**1. Anchoring bias — the agent "remembers" the wrong familiar fault on a shared app.**
For `incorrect_port_assignment` (AstronomyShop, `workload02`), 24 of 26 failed diagnoses (92%)
all misattribute the root cause to *"product-catalog OTLP endpoint misconfiguration"* — a
completely different fault, which is actually the correct root cause for a different
problem_id (`missing_env_variable_astronomy_shop`) run on a different cluster. The agent appears
to pattern-match to a memorized "signature" fault for this app rather than reasoning from the
evidence actually present. Example excerpt from a real failed submission:

> "Diagnosis: product-catalog pods are logging exporter timeouts... OTEL_EXPORTER_OTLP_ENDPOINT
> is set to `http://$(OTEL_COLLECTOR_NAME):4317`..."
> — actual submission for an `incorrect_port_assignment` run (wrong root cause)

This is worth checking across other AstronomyShop-based problem_ids before trusting their
diagnosis-accuracy numbers at face value.

**2. Correct diagnosis, mitigation still fails — the "scheduling gap."**
`unschedulable_incorrect_port_assignment`: diagnosis is correct 89% of the time (24/28), but
mitigation only succeeds 4% of the time (1/23 with both labels recorded) — the largest
diagnosis/mitigation gap in the dataset. `Mitigation.reason` is `null` on all 23 failures, so it
is not yet clear whether this reflects a genuine agent capability gap or an oracle/verification
issue specific to this problem. **Recommend a manual trajectory review before trusting this
problem_id's numbers.**

**3. Reverse exception — `rbac_misconfiguration` mitigates correctly despite wrong diagnosis.**
3/3 mitigation successes despite only 1/3 correct diagnoses. Even when the stated root cause is
imprecise, the applied fix (loosening the RBAC binding) was broad/correct enough for the oracle
to confirm cluster health. This trajectory is still minable for SFT, since the pipeline currently
filters only on `mitigation_success`.

---

## B. Trajectory data status for SFT

186 valid runs span 18/117 registered problem_ids (15.4% catalog coverage). After filtering on
`mitigation_success = True` (the SFT pipeline's mining criterion), **63 clean trajectories**
remain.

### Data concentration is still the main bottleneck

| problem_id | Clean trajectories (mit=True) | % of total |
|---|---|---|
| `incorrect_image` | 22 | 34.9% |
| `update_incompatible_correlated` | 11 | 17.5% |
| `faulty_image_correlated` | 8 | 12.7% |
| 6 other problem_ids (3 each, latest diversity batch) | 18 | 28.6% |
| 2 other problem_ids (1–2 each) | 3 | 6.3% |

The **3 oldest, most-repeated problem_ids account for 65.1%** (41/63) of all clean trajectories
currently available, despite three diversification batches already run.

**6 of the 18 attempted problem_ids (33%) have contributed zero clean trajectories so far:**
`astronomy_shop_payment_service_failure`, `expired_tls_hotel_reservation`,
`k8s_target_port-misconfig`, `missing_env_variable_astronomy_shop`, `network_policy_block`,
`wrong_service_selector_astronomy_shop` — all either in the Network/Security/TLS family or only
run once.

### Live batch at time of writing

At pull time, all 5 `sregym-runner-*` pods had been running for only ~17 minutes, still assigned
to the **same 5 problem_ids as the 2026-08-21 batch-3 run**
(`expired_tls_hotel_reservation` / `dev_shm_exhaustion_hotel_reservation` /
`astronomy_shop_payment_service_failure` / `wrong_dns_policy_astronomy_shop` /
`taint_no_toleration_social_network`) — i.e. it looks like a repeat of batch 3 rather than a
rotation to new problem_ids.

### Recommendations for the next batch

1. **Rotate to unrun problem_ids** — 99/117 have never been run at all; the current live batch is
   repeating batch 3's exact 5 problem_ids instead of expanding coverage.
2. **Manually review `unschedulable_incorrect_port_assignment`** — the 89% diag / 4% mit gap
   needs to be understood (agent limitation vs. oracle issue) before this problem_id's data is
   trusted or used for SFT.
3. **Fix cost telemetry** — `total_cost_usd` is null on 100% of runs, blocking any $/trajectory
   analysis.
4. **Consider nudging tool usage toward Prometheus/Jaeger** — especially for the
   Network/Security/TLS family, where kubectl-only investigation currently performs worst.

---

*Report generated 2026-08-21 from a live pull of all 5 runner pods' PVC data. To regenerate:
`kubectl -n sregym-runner cp <pod>:/workspace/results/traces.db ...` and
`find /workspace/results -maxdepth 2 -iname "*ALL_results.csv"` per pod, then merge/dedupe on
`(cluster, problem_id, attempt, TTL, TTM, steps)`.*
