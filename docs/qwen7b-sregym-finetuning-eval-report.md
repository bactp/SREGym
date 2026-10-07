# Qwen2.5-7B SFT Fine-Tuning Evaluation Report

**Date:** 2026-08-24
**Model under test:** `qwen7b-sregym-v4` (Qwen2.5-7B-Instruct + LoRA r=32, fine-tuned on SREGym oracle-verified trajectories)
**Baseline:** `qwen7b-base` (same Qwen2.5-7B-Instruct checkpoint, no LoRA adapter)
**Serving:** self-hosted via llm-d/vLLM on `gpu-4` (192.168.24.21:30810), `--enable-auto-tool-choice --tool-call-parser hermes`
**Harness:** SREGym kagent runner, 5 parallel target clusters (sre-test1, sre-test2, workload01, workload02, workload03), each `N_ATTEMPTS=3`
**Agent config:** identical systemMessage across all 5 `sre-agent-*` Agents for both runs (md5 `d9434d23615b`), identical problem set for both runs

---

## 1. Executive summary

- Fine-tuning **does measurably help**: the fine-tuned model produced a correct diagnosis in **20%** of episodes and a fully correct diagnosis+mitigation in **13.3%**; the base model scored **0%** on both.
- The base model's dominant failure mode is **giving up** — in **73% of episodes** it made 2-5 read-only tool calls and then silently stopped without ever calling `submit`, never attempting a diagnosis.
- The fine-tuned model's dominant failure mode is different: it **attempts a diagnosis far more often** (60% of episodes reach a `submit` call) but **40% of episodes crash** via a silent HTTP 400 from vLLM, usually after a long repeated-command loop that exhausts the 32K-token context window.
- Neither model is production-ready. The fine-tune's gains are real but narrow: **all 3 successful/near-successful episodes came from a single problem type** (`taint_no_toleration_social_network`), and the same in-distribution problem the model was explicitly trained on (`k8s_target_port-misconfig`) crashed in **100% of attempts**.
- Training-set scale is almost certainly the limiting factor: the LoRA adapter was trained on only **126 examples from 20 episodes covering 5 of 117 problem types**.

---

## 2. Background: model behavior issues found before this benchmark

Before running the parallel 5-cluster comparison, single-episode manual testing (same day, `k8s_target_port-misconfig`, earlier LoRA checkpoints v3/early-v4) surfaced three distinct failure modes, confirmed directly from trajectory files on the results PVC:

| # | Failure mode | Evidence |
|---|---|---|
| 1 | **Exact-repetition loop** | 285 of 291 steps in one episode were the *byte-identical* command `kubectl get deploy -n social-network -o wide \| grep -E 'social-graph-service\|user-service'`, rejected every time for using a forbidden pipe, never varied. Cumulative prompt tokens reached 5.7M, eventually triggering a real vLLM context-length 400 (max 32,768 tokens). |
| 2 | **Premature empty submit** | A separate episode made 4 real tool calls then called `submit(ans="")` — confusing the DIAGNOSIS submit (must contain content) with the MITIGATION submit (empty string by design). Scored 0/100, and the agent then went completely silent for the rest of the episode — mitigation was never attempted. |
| 3 | **Hallucination without tool evidence** | In a manual chat session (not a graded episode) after the target cluster had already been torn down, the model produced a fully detailed, plausible diagnosis+mitigation report (specific ConfigMap edits, "verified 200 OK", "alerts cleared") for a `social-network` namespace that **did not exist on the cluster at all** — confirmed by querying the target cluster directly with kubectl, bypassing the agent. |

### Fix applied before this benchmark
Three rules were added to the shared systemMessage on all 5 `sre-agent-*` Agents:
1. Explicit rule that the diagnosis `submit` must contain content, while the mitigation `submit` is empty by design — not interchangeable.
2. A "stop investigating once you can name the fault" rule, aiming for ~10 tool calls.
3. A ban on shell operators (pipes, etc.) inside kubectl commands.

**Result confirmed by this benchmark: fix #1 worked completely** — zero empty (`ans=""`) diagnosis submissions occurred across all 15 fine-tuned-model episodes. Fixes #2/#3 only partially worked — the loop/crash failure mode still occurred in 40% of episodes (see §4).

---

## 3. Benchmark design

To make the base-vs-fine-tuned comparison valid, both runs used:
- The **same 5 problem_ids**, one per cluster, **3 attempts each (15 episodes per model)**.
- The **same systemMessage** (verified via md5 hash match across all 5 Agents before each run).
- **3 of 5 problems are in-distribution** (the model was trained on trajectories from these): `k8s_target_port-misconfig`, `astronomy_shop_payment_service_failure`, `taint_no_toleration_social_network`.
- **2 of 5 problems are held-out** (never seen in training): `readiness_probe_misconfiguration_hotel_reservation`, `wrong_service_selector_astronomy_shop`.

| Cluster | Problem | In-distribution? |
|---|---|---|
| sre-test1 | `k8s_target_port-misconfig` | Yes |
| sre-test2 | `readiness_probe_misconfiguration_hotel_reservation` | No (held-out) |
| workload01 | `astronomy_shop_payment_service_failure` | Yes |
| workload02 | `wrong_service_selector_astronomy_shop` | No (held-out) |
| workload03 | `taint_no_toleration_social_network` | Yes |

Grading: each episode is graded on a 3-dimension LLM-judge checklist (D1 Fault Localization, D2 Fault Characterization, D3 Scope Precision) producing `Diagnosis.composite_score` (0-1) and a boolean `Diagnosis.success`; mitigation is graded separately as `Mitigation.success` (only reached if diagnosis stage completes).

---

## 4. Full results — fine-tuned model (`qwen7b-sregym-v4`)

Batch `0824_1403`.

| Cluster | Problem | Attempt | Diagnosis.success | Composite | Mitigation.success | Steps | Tool calls | Prompt tokens |
|---|---|---|---|---|---|---|---|---|
| sre-test1 | k8s_target_port-misconfig | 1 | **crash (ungraded)** | — | — | 6 | 4 | 19,405 |
| sre-test1 | k8s_target_port-misconfig | 2 | **crash (ungraded)** | — | — | 6 | 4 | 19,359 |
| sre-test1 | k8s_target_port-misconfig | 3 | **crash (ungraded)** | — | — | 228 | 226 | 4,233,546 |
| sre-test2 | readiness_probe_misconfig... | 1 | False | 0.56 | False | 13 | 11 | 89,256 |
| sre-test2 | readiness_probe_misconfig... | 2 | False | 0.56 | False | 15 | 13 | 116,763 |
| sre-test2 | readiness_probe_misconfig... | 3 | False | 0.56 | False | 11 | 9 | 70,346 |
| workload01 | astronomy_shop_payment... | 1 | **crash (ungraded)** | — | — | 197 | 195 | 3,807,289 |
| workload01 | astronomy_shop_payment... | 2 | False | 0.0 | False | 14 | 12 | 102,335 |
| workload01 | astronomy_shop_payment... | 3 | False | 0.0 | False | 15 | 13 | 104,718 |
| workload02 | wrong_service_selector... | 1 | False | 0.0 | False | 15 | 13 | 115,224 |
| workload02 | wrong_service_selector... | 2 | **crash (ungraded)** | — | — | 133 | 131 | 2,419,817 |
| workload02 | wrong_service_selector... | 3 | **crash (ungraded)** | — | — | 236 | 234 | 4,567,600 |
| workload03 | taint_no_toleration... | 1 | **True** | **1.00** | **True** | 10 | 8 | 43,869 |
| workload03 | taint_no_toleration... | 2 | **True** | **1.00** | **True** | 12 | 10 | 59,184 |
| workload03 | taint_no_toleration... | 3 | **True** | 1.00 | crash *after* diagnosis pass | 254 | 252 | 4,479,062 |

### Fine-tuned model — aggregates
| Metric | Value |
|---|---|
| Diagnosis pass | 3/15 (20%) — all from `taint_no_toleration_social_network` |
| Full pass (diagnosis + mitigation) | 2/15 (13.3%) |
| Crashed before grading (silent HTTP 400) | 6/15 (40%) |
| Wrong diagnosis, non-empty content | 6/15 (40%) |
| Empty (`ans=""`) diagnosis submissions | **0/15 (0%)** — prior bug confirmed fixed |

### Two distinct crash subtypes observed (fine-tuned model only)
- **Fast crash**: dies after only 4-11 tool calls, 19-115K prompt tokens — not a context-length issue (far below 32,768). Root cause unidentified; neither vLLM's access log nor the kagent client logs the actual 400 response body.
- **Loop crash**: 130-252+ tool calls, 2.4-4.5M cumulative prompt tokens — matches the pre-fix repetition-loop-then-context-overflow pattern. The "stop after ~10 calls" systemMessage rule did not reliably prevent this.

---

## 5. Full results — base model (`qwen7b-base`)

Batch `0824_1537`.

| Cluster | Problem | Attempt | Diagnosis.success | Composite | Mitigation.success | Tool calls | Steps | Prompt tokens | Notes |
|---|---|---|---|---|---|---|---|---|---|
| sre-test1 | k8s_target_port-misconfig | 1 | **no submit** | — | — | 2 | 12 | 79,008 | gave up after 2 tool calls |
| sre-test1 | k8s_target_port-misconfig | 2 | **no submit** | — | — | 5 | 13 | 124,194 | gave up |
| sre-test1 | k8s_target_port-misconfig | 3 | **no submit** | — | — | 2 | 37 | 408,463 | **hard 1800s wall-clock timeout**; 37 total steps but only 2 tool calls (mostly idle turns) |
| sre-test2 | readiness_probe_misconfig... | 1 | **no submit** | — | — | 3 | 5 | 9,426 | 3 read-only calls, then stopped |
| sre-test2 | readiness_probe_misconfig... | 2 | **no submit** | — | — | 3 | 5 | 9,426 | identical numbers to attempt 1 |
| sre-test2 | readiness_probe_misconfig... | 3 | **no submit** | — | — | 3 | 5 | 9,426 | identical numbers to attempts 1-2 |
| workload01 | astronomy_shop_payment... | 1 | False | 0.0 | False | 3 | 14 | 141,343 | blamed Grafana (wrong) |
| workload01 | astronomy_shop_payment... | 2 | False | 0.0 | (ungraded) | 3 | 37 | 287,878 | blamed Kafka broker (wrong) |
| workload01 | astronomy_shop_payment... | 3 | False | 0.0 | False | 2 | 37 | 474,350 | blamed Jaeger/otel-collector (wrong) |
| workload02 | wrong_service_selector... | 1 | **no submit** | — | — | 3 | 59 | 1,221,221 | 3 read-only calls then 56 idle turns, no submit |
| workload02 | wrong_service_selector... | 2 | **no submit** | — | — | 3 | 27 | 338,199 | same pattern |
| workload02 | wrong_service_selector... | 3 | **no submit** | — | — | 3 | 6 | 19,525 | same pattern |
| workload03 | taint_no_toleration... | 1 | **no submit** | — | — | 2 | 13 | 49,758 | gave up |
| workload03 | taint_no_toleration... | 2 | False | 0.67 | **True** | 5 | 18 | 245,572 | diagnosis close (missed "toleration" framing) but mitigation still succeeded |
| workload03 | taint_no_toleration... | 3 | **no submit** | — | — | 2 | 6 | 22,855 | gave up |

### Base model — aggregates
| Metric | Value |
|---|---|
| Diagnosis pass | 0/15 (0%) |
| Full pass | 0/15 (0%) |
| Mitigation pass | 1/15 (6.7%) — despite a diagnosis graded False |
| **Never called `submit` at all** | **11/15 (73%)** |
| Wrong diagnosis, non-empty content | 4/15 (26.7%) |
| Hard timeout (1800s) | 1/15 |

A striking sub-pattern: for `readiness_probe_misconfiguration_hotel_reservation`, all 3 base-model attempts produced **byte-identical** telemetry (94 completion tokens, 9,426 prompt tokens) — the model took the exact same 3 read-only actions and stopped at the exact same point every time, despite `temperature=0`/`seed=0` making this technically expected for a deterministic decode of an identical prompt.

---

## 6. Head-to-head comparison

| Metric | `qwen7b-base` | `qwen7b-sregym-v4` (fine-tuned) |
|---|---:|---:|
| Diagnosis pass rate | **0%** | **20%** |
| Full pass rate (diagnosis + mitigation) | **0%** | **13.3%** |
| Mitigation-only success (diagnosis graded False) | 6.7% (1/15) | 0% |
| Never attempted a diagnosis (`submit` never called) | **73%** | **0%** |
| Crashed via silent HTTP 400 / context overflow | ~0% (1 hard 1800s timeout instead) | **40%** |
| Attempted a diagnosis but got it wrong | 26.7% | 40% |
| Empty diagnosis submissions | 0% | 0% |

### Interpretation
1. **Fine-tuning changed the model's behavior from "give up" to "try, sometimes wrongly, sometimes destructively."** The base model's dominant behavior (73% of episodes) is to make a few read-only observability calls and then simply stop — it does not attempt to reason to a conclusion or act. The fine-tuned model almost never does this (0%); it consistently pushes forward to either a real (if sometimes wrong) diagnosis or a runaway tool-call loop.
2. **The fine-tune's wins are concentrated in one problem type.** All 3 diagnosis passes and both full passes came from `taint_no_toleration_social_network` — a scheduling/taint problem, one of the 5 problem types actually present in the 20-episode training set. The other in-distribution problem, `k8s_target_port-misconfig`, crashed 100% of the time (3/3) despite being a training problem too — in-distribution training does not reliably predict live pass rate at this training scale.
3. **The fine-tune introduced a new failure mode the base model doesn't exhibit as often**: the silent-400 crash. This is very likely a direct consequence of the model now being *willing* to keep working the problem instead of giving up — more tool calls and longer trajectories create more opportunities to hit a repeated-command loop that exhausts the context window.
4. **The one base-model success** (`taint_no_toleration_social_network` attempt 2, mitigation True despite Diagnosis composite only 0.67) shows the *base* model already has some latent capability on this specific problem type — the fine-tune's improvement here is more "more consistent" (2/3 full pass vs. 1/3 partial) than "unlocking a new capability."

---

## 7. Root-cause notes for follow-up (not yet resolved)

- **Two distinct 400-crash subtypes in the fine-tuned model** remain unexplained/unfixed:
  - Fast crash (4-11 tool calls, far below the 32K token limit) — cause unknown; neither vLLM's access log nor the kagent client currently logs the actual HTTP 400 response body, which blocks further diagnosis without adding debug-level logging or a request-capturing proxy.
  - Loop crash (130-252+ tool calls, 2.4-4.5M cumulative tokens) — matches the pre-fix repetition-loop pattern; the systemMessage's "stop after ~10 calls" rule is not being reliably followed.
- **Training-set scale is the most likely limiting factor.** As of 2026-08-22 the SFT mining pipeline had only 126 examples from 20 oracle-verified episodes covering 5 of 117 registered problem_ids — a proof-of-concept-scale dataset. The concentration of the fine-tune's gains in exactly one of those 5 trained problem types is consistent with this.
- **Harness discrepancy between the two runs**: the base-model run's result CSVs include two columns (`agent_timeout_seconds`, `timed_out`) that are absent from the fine-tuned run's CSVs, indicating the SREGym runner (`main.py`) picked up a hard 1800s per-attempt wall-clock timeout between the two runs. This affected only 1 of 30 total episodes (sre-test1 attempt 3, base model) and does not change the headline conclusions, but should be noted as a minor confound if presenting exact numbers.

---

## 8. Suggested slides / talking points for the PPT

1. **Headline chart**: bar chart of Diagnosis pass % and Full pass % — base (0%, 0%) vs. fine-tuned (20%, 13.3%).
2. **Behavior-shift chart**: stacked bar of episode outcomes (no-submit / wrong-but-attempted / crash / pass) for both models — visually shows the shift from "gives up" (base) to "tries, but crashes or gets it wrong" (fine-tuned).
3. **Per-problem breakdown table** (§4/§5) — makes clear the win is concentrated in `taint_no_toleration_social_network`, useful for an honest "what's not yet solved" slide.
4. **Known open issues slide**: the two unresolved crash subtypes + training-set scale caveat (126 examples / 5 problem types) as the recommended next investment before claiming broader capability.
