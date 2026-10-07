# SREGym → SFT Data Pipeline: Where the Data Lives, How It's Labeled, How to Get Fine-Tuning-Ready Data

Guide for the training-side agent (e.g. the one running on `gpu-4 / 192.168.24.21`)
that will pull SREGym trace data and fine-tune **Qwen2.5-7B-Instruct** on it.

Everything below is verified against the real repo state on `bactp@dcn.ssu.ac.kr`'s
`SREGym` checkout (branch `SFT`, commit `d17d85c7`) on 2026-08-22 — commands were
actually executed, not guessed.

---

## 1. Where the raw data currently lives

**Correction (2026-08-22): the storage backend is Longhorn, not MinIO/S3.**
An earlier draft of this guide implied local disk only — that was incomplete.
When runs are produced by the parallel K8s-Job runner (`deploy/runner/`,
the normal way to generate new episodes at scale), the actual origin is:

- **PVC `sregym-results`**, namespace `sregym-runner`
  (`deploy/runner/k8s/pvc.yaml`), `ReadWriteMany`, 20Gi.
- **StorageClass `sregym-runner-results`** (`deploy/runner/k8s/storageclass.yaml`),
  `provisioner: driver.longhorn.io`, `numberOfReplicas: "2"` (deliberately
  dropped from Longhorn's cluster default of 3 — the mgmt cluster only has 2
  Longhorn-schedulable worker nodes; see the file's own comment).
- Each parallel Job mounts this **same** PVC into `/workspace` with
  `subPathExpr: "$(JOB_NAME)"` (`deploy/runner/k8s/job-base/job.yaml`), so
  concurrent Jobs write to non-colliding subdirectories of one shared RWX
  volume instead of each getting a separate disk.
- Confirmed explicitly in `docs/parallel-runner-guide.md:26`: *"PVC
  sregym-results (Longhorn RWX — where trajectories land)"*, and its §4 step 6
  for reading results back out: `ls
  /pvc-root/sregym-runner-cluster-a/results/<timestamp>/<agent>/<problem>/run_1/`
  from any pod/debug-pod with the PVC mounted at its root.
- **There is no MinIO/S3 anywhere in this repo as a storage backend.**
  `boto3` appears only as a declared dependency (`pyproject.toml`,
  `docker/agents/requirements-container.txt`) — grepping all `.py` files for
  `import boto3` / `minio` turns up nothing; it's unused. Don't configure an
  S3 endpoint on the GPU-4 side — there's nothing to point it at.
- Separately, **observability data** (Prometheus/Loki metrics & logs on the
  workload clusters) uses a *different* StorageClass, `openebs-hostpath`
  (`sregym/observer/prometheus/`, `sregym/observer/loki/loki-values.yaml`) —
  that's monitoring data, not benchmark/trace data, and irrelevant to SFT
  mining.

So getting fresh trace data onto GPU-4 means either (a) copying the aggregated
`results/traces.db` from wherever it was already built (e.g. `bactp`'s
checkout, as in §7 below), or (b) if pulling straight from a live run, reading
it off the Longhorn PVC via a debug pod as shown above, then running
`sregym.traces.postprocess` + `sregym.traces.store ingest` yourself (§5 of
`docs/parallel-runner-guide.md`). Either way, **the file transfer is always
pod-exec/scp of a PVC-backed path or a local SQLite file — never an S3 URL.**

Three layers after that point, each derived from the one before it. Nothing
is fabricated — every layer traces back to real agent runs against real
Kubernetes clusters.

```
results/<batch>/<tool>/<problem_id>/run_<n>/     <- Layer 1: raw per-run artifacts
        │
        ▼  sregym/traces/convert.py + sregym/traces/postprocess
results/<batch>/<tool>/<problem_id>/run_<n>/trajectory.json   <- Layer 2: ATIF-normalized trajectory
        │
        ▼  sregym/traces/store.py  (python -m sregym.traces.store ingest)
results/traces.db                                <- Layer 3: SQLite index of ALL trajectories
        │
        ▼  sregym/traces/sft/mine.py
train.jsonl                                       <- Layer 4: SFT training examples
```

**Layer 1 — raw run directory** (example, real path):
`results/0727_0736/kagent/k8s_target_port-misconfig/run_1/`
```
sregym_0727_0744.log                                    # driver log
0727_0745_k8s_target_port-misconfig_kagent_invoke_result.json   # raw KAgent tool-call log
k8s_target_port-misconfig_results.csv                    # ORACLE verdict (see §3)
trajectory.json                                          # ATIF-normalized trajectory (Layer 2)
```
The path itself is meaningful and parsed by `sregym/traces/convert.py::parse_run_path`:
`results/<batch>/<tool>/<problem_id>/run_<n>/` where `batch` is a timestamp
directory (`MMDD_HHMM`), `tool` is the agent (`kagent`, `kagent-orchestrator`,
`stratus`, `claudecode`, `codex`, ...), `problem_id` is the fault scenario
(117 registered, see `docs/problem-catalog.md`), `run_<n>` is the attempt number.

**Layer 3 — the actual DB right now:** `results/traces.db` (1.9 MB, SQLite).
Current contents (`python -m sregym.traces.store --db results/traces.db stats`):
```
total: 26 root trajectories
by problem type:
  k8s_target_port-misconfig: 11
  pod_anti_affinity_deadlock: 8
  mutating_webhook_resource_limits_social_network: 4
  liveness_probe_too_aggressive_social_network: 2
  service_dns_resolution_failure_social_network: 1
by agent:
  kagent: 24
  stratus: 2
```
⚠️ **Important**: only **26 episodes total**, covering **5 of 117** registered
problem_ids. This is not yet a large dataset — see §6 for what that means for
fine-tuning readiness.

`results/traces.db` is **gitignored** — it's a local build artifact, not checked
into the repo. You must transfer this file itself (or the underlying
`results/` tree + rebuild it) to the GPU node; cloning the repo alone will not
bring it over.

---

## 2. Layer 2 structure: ATIF trajectory

`trajectory.json` follows the "Agent Trajectory Interchange Format" (ATIF),
built by the `atif_converter` package. Structure (Pydantic models in
`atif_converter/atif/`):

```
Trajectory
├── trajectory_id        # = the canonical results_path, e.g. "0727_0736/kagent/k8s_target_port-misconfig/run_1"
├── agent: {name, version, model_name, tool_definitions}
├── steps: [Step, ...]
│     ├── step_id, source ("agent" | "environment" | ...), timestamp
│     ├── message (assistant text) / reasoning_content
│     ├── tool_calls: [{tool_call_id, function_name, arguments}, ...]
│     ├── observation: {results: [{content, source_call_id}, ...]}
│     └── metrics: {prompt_tokens, completion_tokens, cost_usd, ...}
├── final_metrics: {total_prompt_tokens, total_cost_usd, total_steps, ...}
├── subagent_trajectories: [Trajectory, ...]   # for orchestrator agents
└── extra.sregym: {                            # SREGym-specific metadata (§3)
      problem_id, application, run, results_path,
      submitted, diagnosis_submitted_step,
      diagnosis_success, mitigation_success,   # <- the ORACLE labels
      ttl_seconds, ttm_seconds
    }
```

`results/traces.db` is a normalized relational mirror of this (tables
`trajectories` / `steps` / `tool_calls` / `observation_results` — see
`sregym/traces/store.py`). It's a rebuildable index, not a second source of
truth — the `trajectory.json` files remain canonical. Rebuild it any time with:
```bash
python -m sregym.traces.store ingest results/ --db results/traces.db
```

---

## 3. How labeling works (the ground truth)

Two **independent** notions of "success" exist — do not confuse them:

1. **`submitted`** — did the agent's CLI process exit 0? Comes from the tool's
   own `*_results_*.json` (client driver exit code). **Not a quality signal**,
   just "did it crash."
2. **`diagnosis_success` / `mitigation_success`** — the real, oracle-verified
   ground truth. Written by SREGym's **conductor** (the environment harness),
   which independently evaluates the agent's diagnosis text and the live
   cluster's post-mitigation state against a fixed rubric — see the real
   example at `results/0727_0736/kagent/k8s_target_port-misconfig/run_1/k8s_target_port-misconfig_results.csv`:
   a checklist of yes/no questions (fault localization, characterization,
   scope precision) scored by an LLM judge, reduced to `Diagnosis.success` /
   `Mitigation.success` booleans plus `TTL`/`TTM` (time-to-diagnose /
   time-to-mitigate, seconds). This CSV is parsed into
   `extra.sregym.{diagnosis_success,mitigation_success,ttl_seconds,ttm_seconds}`
   by `sregym/traces/convert.py::build_sregym_meta`.

**The SFT pipeline only mines episodes where `mitigation_success == True`**
(oracle-verified) — see `sregym/traces/sft/mine.py::mine()`. Failed/unverified
episodes are never used as positive training examples in the current
pipeline (there is a `negative_invalid_action` task type, but that labels
individual **environment-rejected tool calls** within an otherwise-successful
episode, not whole failed episodes — see below).

### Sub-labeling within a successful episode: milestones

Within one oracle-verified trajectory, `sregym/traces/sft/detectors.py` +
`sregym/traces/sft/problems/*.py` assign each **agent step** to an
operational **milestone**, which maps to one of six canonical **roles**
(`MilestoneRole` enum): `OBSERVE → EVIDENCE → DIAGNOSIS → MITIGATION_ACTION →
POST_ACTION_CHECK → MITIGATION_SUBMITTED`.

Critically, this is **content-based, not position-based**: a detector looks
at the actual tool name / command string / observation text of a step (e.g.
"does this look like `kubectl get svc`", "does the observation contain
`Connection refused`") — never "step number 5". This is what lets the same
detector segment two runs of the same problem that took a completely
different number of steps (a flat agent doing 24 raw kubectl steps vs. an
orchestrator doing 6 steps that delegate to opaque sub-agents). See
`sregym/traces/sft/problems/target_port_misconfig.py` for a hand-tuned
example and `sregym/traces/sft/problems/generic_kagent.py` for the generic
fallback (verb-based: `get/describe/logs` = investigative,
`patch/apply/delete/scale/...` = mitigation, `submit` with a non-empty `ans`
argument = diagnosis submission, empty `ans` = mitigation submission).

Only **`k8s_target_port-misconfig`** currently has a hand-tuned, per-problem
detector authored from real runs. Every other problem_id (including the 112
that have **zero** real runs recorded yet) falls back to
`generic_kagent.py`'s heuristic, which was verified against 5 problems'
worth of real runs but is unverified for the rest.

---

## 4. Layer 4: how `train.jsonl` is generated and what's in it

Command (from repo root, with `results/traces.db` present):
```bash
python -m sregym.traces.sft.mine results/traces.db train.jsonl --category k8s_service_networking
```
Optional `--problem <problem_id>` restricts to one problem type.

This runs `sregym/traces/sft/cut.py::build_records` on every oracle-verified
(`mitigation_success=True`) trajectory that has a registered detector, and
emits **up to 6 example types per episode** (fewer if a role wasn't reached —
e.g. an orchestrator run whose sub-agent hides the evidence-gathering steps
never yields `tool_selection`):

| `task` | What it teaches | Built from |
|---|---|---|
| `tool_selection` | Given context, pick the next investigative tool call | boundary between two consecutive OBSERVE/EVIDENCE milestones |
| `state_interpretation` | Evidence text → root-cause diagnosis text | real EVIDENCE observations → real `submit(ans=...)` diagnosis text |
| `action_selection` | Confirmed diagnosis → the real mitigating tool call | DIAGNOSIS text → first MITIGATION_ACTION tool call |
| `verification` | After a fix, choose the real re-check action | two consecutive POST_ACTION_CHECK tool calls |
| `negative_invalid_action` | Recognize environment-rejected actions (labeled `"label": "rejected_by_environment"`) | any real step whose observation matched a rejection pattern (`Command Rejected`, `Forbidden`, `permission denied`, ...) |
| `full_trajectory` | End-to-end imitation of the whole successful episode | every step, agent text kept verbatim |

**Verified real output** (ran on the actual `results/traces.db` today):
`126 examples from 20 episodes` — `{'tool_selection': 19, 'state_interpretation': 13, 'action_selection': 17, 'verification': 13, 'negative_invalid_action': 44, 'full_trajectory': 20}`.

### Record shape (real example, `action_selection`)
```json
{
  "sample_id": "0727_0736/kagent/k8s_target_port-misconfig/run_1_action_selection",
  "source": "sregym",
  "episode_id": "0727_0736/kagent/k8s_target_port-misconfig/run_1",
  "task": "action_selection",
  "category": "k8s_service_networking",
  "problem": "k8s_target_port-misconfig",
  "oracle": {"diagnosis_success": true, "mitigation_success": true},
  "step_range": [17, 19],
  "messages": [
    {"role": "system", "content": "You are a Kubernetes infrastructure operations model.\n\nWorkflow:\n1. ..."},
    {"role": "user", "content": "Confirmed diagnosis: Diagnosis: Multiple services are failing RPCs with \"Connection refused\" to user-service. ...\n\nApply the fix."},
    {"role": "assistant", "tool_calls": [{"name": "exec_kubectl_cmd_safely", "arguments": {"cmd": "kubectl patch svc -n social-network user-service -p '{\"spec\":{\"ports\":[{\"name\":\"9090\",\"port\":9090,\"protocol\":\"TCP\",\"targetPort\":9090}]}}' --type=merge"}}]}
  ]
}
```
Notes:
- **Metadata (`sample_id`, `oracle`, `problem`, ...) is deliberately kept
  outside `messages`** — nothing but real conversation content is in the
  trained field. Don't feed the metadata fields to the model.
- `messages[i].role` is one of `system` / `user` / `assistant` / `tool`.
- `assistant` messages carry `tool_calls: [{"name", "arguments"}]` — **this is
  NOT the OpenAI wire format** (no `id`, no `type: "function"`, no nested
  `function.arguments` JSON-string). You must transform this before feeding
  it to a standard SFT trainer / chat template (see §5).
- `tool` messages carry a flattened `content` string (tool output text,
  truncated to 25 lines per observation via `MAX_OBSERVATION_LINES`) — there
  is no `tool_call_id` linkage field, so multi-tool-call-per-step is not
  disambiguated at this layer. In practice each step has 0 or 1 tool call.

---

## 5. Turning `train.jsonl` into fine-tuning-ready data for Qwen2.5-7B-Instruct

`train.jsonl` is a **staging format**, not something you can point a trainer
at directly — Qwen2.5's chat template expects OpenAI/Hermes-style tool
calling. You need one conversion pass. Two options, pick based on what
training framework the GPU-4 agent uses:

### Option A — LLaMA-Factory / axolotl `sharegpt`-with-tools style
Convert each record's `messages` into the framework's expected shape. The
mechanical transform, in Python:

```python
import json, uuid

def to_qwen_format(rec):
    out = []
    for m in rec["messages"]:
        if m["role"] == "assistant" and "tool_calls" in m:
            out.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
                    }
                    for i, tc in enumerate(m["tool_calls"])
                ],
            })
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"]})
        else:
            out.append(m)
    return {"messages": out}

with open("train.jsonl") as fin, open("train.qwen.jsonl", "w") as fout:
    for line in fin:
        rec = json.loads(line)
        fout.write(json.dumps(to_qwen_format(rec), ensure_ascii=False) + "\n")
```
This produces the standard OpenAI-messages-with-tool_calls JSONL that
LLaMA-Factory's `sharegpt`/`openai` dataset format and axolotl's
`chat_template` dataset type both accept directly (point their dataset
config at the Qwen2.5 chat template — `AutoTokenizer.from_pretrained(...,
trust_remote_code=True).apply_chat_template(..., tools=...)` handles the
`<tool_call>`/`<tool_response>` special tokens Qwen2.5 was trained with).

If you also want the model to learn to *emit* tool schemas, register a
`tools` array (JSON-schema function defs) per conversation — SREGym's own
tool definitions live in `agent.tool_definitions` on the ATIF trajectory
(`json(tool_definitions)` column in `traces.db`), which the mining pipeline
does **not** currently thread into `train.jsonl` records. If needed, add a
`tools` field to each record from `trajectory.agent.tool_definitions` before
this conversion.

### Option B — flatten to plain-text ChatML (simplest, framework-agnostic)
If the GPU-4 side just wants raw text completions (e.g. a minimal HF
`Trainer` / `SFTTrainer` script with no tool-calling scaffolding), serialize
tool calls/responses into the assistant/tool text itself using Qwen's own
`<tool_call>{json}</tool_call>` convention, then apply
`tokenizer.apply_chat_template(messages, tokenize=False)` per Qwen2.5's
template and train on the resulting string with the standard
"mask everything except assistant spans" loss convention (most SFT
frameworks — TRL's `SFTTrainer` with `DataCollatorForCompletionOnlyLM`, or
axolotl's default — do this for you once `role` is set correctly).

### Split / dedup considerations before training
- **Split by `episode_id`, never by record.** Six examples can come from the
  same episode (e.g. all 6 task types from one `k8s_target_port-misconfig`
  run) — putting `tool_selection` from an episode in train and
  `verification` from the *same* episode in val leaks the episode's specific
  scenario across the split.
- **Current class imbalance**: with only 20 mined episodes, `problem` is
  extremely skewed toward `k8s_target_port-misconfig` (11/26 raw episodes)
  and `pod_anti_affinity_deadlock` (8/26) — expect the fine-tune to
  overfit to these two scenarios unless more runs are collected first (see §6).
- `negative_invalid_action` records carry `"label": "rejected_by_environment"`
  — if you're doing plain SFT (not DPO/preference tuning), decide up front
  whether to include these at all; they teach "recognize this was rejected",
  not "here is the ideal action."

---

## 6. Honest caveat before spending GPU-4 time

As of 2026-08-22 the mineable dataset is **126 examples from 20 real
episodes across only 5 of 117 registered problem_ids** — this is a
proof-of-concept-scale dataset, not yet enough for a robust general-purpose
K8s-ops fine-tune of a 7B model. Two ways to grow it before/while training:

1. **Run more episodes** via the parallel K8s-Job runner
   (`deploy/runner/README.md`, `docs/parallel-runner-guide.md`) against more
   of the 117 problem_ids, then re-ingest (`python -m sregym.traces.store
   ingest results/ --db results/traces.db`) and re-mine.
2. **Author more per-problem detectors** (`sregym/traces/sft/problems/`) once
   real runs exist for a new problem_id — the generic fallback works but is
   unverified outside the 5 problems it was checked against.

If the goal is just to validate the end-to-end fine-tuning mechanics on
GPU-4 first (tokenization, template, loss masking, throughput) before
scaling up data collection, the current 126-example set is sufficient for
that dry run.

---

## 7. Concrete command sequence to hand to the GPU-4 agent

```bash
# 1. Get the code (needed: sregym/traces/, atif_converter/, and results/traces.db)
git clone <repo-url> SREGym && cd SREGym
git checkout SFT   # or main, once merged

# 2. Pull the actual data (traces.db is gitignored — transfer it explicitly)
scp <source-host>:/home/ubuntu/SREGym/results/traces.db ./results/traces.db
# (or: rsync the whole results/ tree if you want raw trajectory.json access too)

# 3. Python env
python3 -m venv .venv && source .venv/bin/activate
pip install -e .   # pulls in pydantic etc. required by atif_converter / sregym.traces

# 4. Mine SFT examples from the oracle-verified trajectories
python -m sregym.traces.sft.mine results/traces.db train.jsonl -v
# inspect: wc -l train.jsonl ; jq -s 'group_by(.task)|map({task:.[0].task,n:length})' train.jsonl

# 5. Convert to Qwen2.5-tool-calling format (Option A script in §5) -> train.qwen.jsonl

# 6. Fine-tune with your framework of choice (LLaMA-Factory / axolotl / TRL),
#    base model Qwen/Qwen2.5-7B-Instruct, chat template = Qwen2.5's own,
#    split by episode_id (§5), loss masked to assistant turns only.
```
