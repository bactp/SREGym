# KAgent Integration — Design, Installation, and Usage

> How [KAgent](https://kagent.dev) (kagent-dev/kagent), a Kubernetes-native AI agent framework,
> was wired into SREGym as an additional `--agent` option alongside `stratus`/`codex`/etc.
> Written 2026-07-27 from the session that built this integration end-to-end and verified it
> against a real problem. Assumes the base SREGym deployment from
> [`docs/deployment.md`](./deployment.md) is already working.

## 1. Architecture

Unlike SREGym's other agent clients (`codex`, `claudecode`, `stratus`, ...), the actual LLM agent
in this integration does **not** run inside SREGym's own process or Docker image. It runs as a
`kagent.dev` `Agent` custom resource on a **separate Kubernetes cluster** — the same management
cluster described in `docs/deployment.md`, not the workload/target cluster being tested.

```
┌─────────────────────────── sre-control (management cluster) ───────────────────────────┐
│                                                                                          │
│  SREGym (host process, uv run python main.py)         kagent (namespace: kagent)        │
│  ├─ conductor REST API :8000 ────────┐                ├─ kagent-controller :8083        │
│  ├─ MCP tool servers    :9954         │                │    (A2A / kagent invoke API)    │
│  │   (kubectl/prometheus/jaeger)      │                ├─ Agent CRD: sre-agent ──────┐   │
│  └─ clients/kagent/driver.py ─────────┤                │    (ModelConfig: OpenAI)    │   │
│       "kagent invoke --agent          │                └─ RemoteMCPServer CRDs x4    │   │
│        sre-agent --task ..."          │                     (kubectl/prometheus/     │   │
│                                       │                      jaeger/submit) ─────────┤   │
│                                       └─── forwarded node-IP:port ◄───────────────────┘   │
│                                            (192.168.28.158:9954 / :8000)                  │
└──────────────────────────────────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
                                  sre-test1 (workload/target cluster)
                                  — the actual Kubernetes app being
                                    diagnosed/mitigated, reached by
                                    the kubectl MCP tool server's
                                    own KUBECONFIG (unrelated to
                                    the management-cluster kubeconfig)
```

**Key design decision:** rather than writing a bespoke tool-execution layer for KAgent, the
`sre-agent` Agent CRD is wired (via `RemoteMCPServer` CRDs) to consume SREGym's *existing*
kubectl/prometheus/jaeger/submit MCP tool servers — the exact same ones `stratus` and `demo`
already use. This means:
- No new tool code, no new oracle/scoring logic — the conductor evaluates a KAgent submission
  identically to any other agent's.
- SREGym's own code changes are pure glue: install KAgent, point it at the existing tool
  servers, and add a thin driver that kicks off `kagent invoke` and waits for completion.

The trade-off: those MCP tool servers are SSE endpoints that SREGym only stands up (via
`kubectl port-forward ... --address 0.0.0.0`) for the duration of an active benchmark run, bound
to the management cluster node's public IP. `sre-agent` can therefore only successfully call
tools *while a SREGym run is in progress* — which is exactly when it's ever invoked, so this is
not a limitation in practice.

## 2. Install KAgent on the management cluster

### 2.1 Dedicated kubeconfig

KAgent must not be installed against the workload/target cluster, and must not disturb
`~/.kube/config` (which SREGym needs pointed at whatever workload cluster is under test). Use a
separate copy of the management cluster's own admin kubeconfig:

```bash
sudo cp /etc/kubernetes/admin.conf /home/ubuntu/mgmt.kubeconfig
sudo chown ubuntu:ubuntu /home/ubuntu/mgmt.kubeconfig
chmod 600 /home/ubuntu/mgmt.kubeconfig
```

Every command below uses `KUBECONFIG=/home/ubuntu/mgmt.kubeconfig` (or `--kubeconfig`
explicitly) — never the SREGym target kubeconfig.

### 2.2 API key handling — ⚠️ do this via a pre-created Secret, not `--set`

The kagent Helm chart supports both a raw `apiKey` value and an `apiKeySecretRef` (pointing at a
pre-existing Secret). **Use the Secret path.** Putting a real API key inline in a `helm --set` or
even in a `kubectl apply -f - <<EOF` heredoc is something an automated agent running these
commands may refuse to do (rightly — it's bad practice regardless), and it's simply safer: the
key never appears in shell history or `helm get values` output.

Create the Secret first (run this yourself in a terminal that already has `OPENAI_API_KEY`
exported, e.g. one that sources `/root/.bashrc`):

```bash
export KUBECONFIG=/home/ubuntu/mgmt.kubeconfig
kubectl create namespace kagent --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: kagent-openai
  namespace: kagent
type: Opaque
stringData:
  OPENAI_API_KEY: "${OPENAI_API_KEY}"
EOF
```

The chart's default OpenAI provider config already expects a secret named exactly
`kagent-openai` with key `OPENAI_API_KEY` (`apiKeySecretRef: kagent-openai` /
`apiKeySecretKey: OPENAI_API_KEY` — confirmed via `helm show values`), so no further wiring is
needed on the Helm side.

### 2.3 Install the charts

```bash
export KUBECONFIG=/home/ubuntu/mgmt.kubeconfig

helm install kagent-crds oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds \
  -n kagent --create-namespace

helm install kagent oci://ghcr.io/kagent-dev/kagent/helm/kagent \
  -n kagent --set providers.default=openAI
```

No `apiKey` flag needed — it resolves the key from the `kagent-openai` Secret created above.

### 2.4 Disable the bundled default agents/tools

The base chart ships ~10 pre-built agents (`k8s-agent`, `istio-agent`, `cilium-*-agent`,
`promql-agent`, `observability-agent`, `argo-rollouts-agent`, `helm-agent`) and extra tool
servers (`grafana-mcp`, `querydoc`) that aren't needed here. Disable them to keep the namespace
lean:

```bash
helm upgrade kagent oci://ghcr.io/kagent-dev/kagent/helm/kagent -n kagent \
  --set providers.default=openAI \
  --set k8s-agent.enabled=false \
  --set kgateway-agent.enabled=false \
  --set istio-agent.enabled=false \
  --set promql-agent.enabled=false \
  --set observability-agent.enabled=false \
  --set argo-rollouts-agent.enabled=false \
  --set helm-agent.enabled=false \
  --set cilium-policy-agent.enabled=false \
  --set cilium-manager-agent.enabled=false \
  --set cilium-debug-agent.enabled=false \
  --set grafana-mcp.enabled=false \
  --set querydoc.enabled=false
```

### 2.5 Install the `kagent` CLI

```bash
curl https://raw.githubusercontent.com/kagent-dev/kagent/refs/heads/main/scripts/get-kagent | bash
export KUBECONFIG=/home/ubuntu/mgmt.kubeconfig
kagent version
```

### 2.6 Verify

```bash
export KUBECONFIG=/home/ubuntu/mgmt.kubeconfig
kubectl get pods -n kagent          # controller, ui, tools, postgres, kmcp-controller — all Running
kubectl get svc  -n kagent          # note kagent-controller:8083
```

**Gotcha we hit:** the bundled Postgres image (`docker.io/library/postgres:18.3-alpine`) can
take several minutes to pull on a slow/throttled link to Docker Hub, while the `ghcr.io` images
pull fast — this cascades into the controller crash-looping (it fails DB migrations until
Postgres is actually up), which looks alarming but resolves itself once the pull finishes. If it
seems stuck, check with `crictl images` / `ctr -n k8s.io image pull ...` directly on the node
Postgres is scheduled to, rather than assuming something is broken.

## 3. Register SREGym's MCP tool servers as `RemoteMCPServer` CRDs

Tool names exposed by each SREGym MCP server (from `mcp_server/*.py`):

| Server | Endpoint | Tools |
|---|---|---|
| kubectl | `:9954/kubectl/sse` | `exec_kubectl_cmd_safely`, `rollback_command`, `get_previous_rollbackable_cmd` |
| prometheus | `:9954/prometheus/sse` | `get_metrics`, `get_alerts` |
| jaeger | `:9954/jaeger/sse` | `get_services`, `get_operations`, `get_traces`, `get_dependency_graph` |
| submit | `:8000/submit_mcp/sse` | `submit` |

```bash
export KUBECONFIG=/home/ubuntu/mgmt.kubeconfig
cat <<'EOF' | kubectl apply -f -
apiVersion: kagent.dev/v1alpha2
kind: RemoteMCPServer
metadata:
  name: sregym-kubectl
  namespace: kagent
spec:
  description: SREGym kubectl-exec MCP tool server (target cluster access)
  protocol: SSE
  url: http://<mgmt-node-ip>:9954/kubectl/sse
  timeout: 60s
  sseReadTimeout: 1h
---
apiVersion: kagent.dev/v1alpha2
kind: RemoteMCPServer
metadata:
  name: sregym-prometheus
  namespace: kagent
spec:
  description: SREGym Prometheus metrics MCP tool server
  protocol: SSE
  url: http://<mgmt-node-ip>:9954/prometheus/sse
  timeout: 60s
  sseReadTimeout: 1h
---
apiVersion: kagent.dev/v1alpha2
kind: RemoteMCPServer
metadata:
  name: sregym-jaeger
  namespace: kagent
spec:
  description: SREGym Jaeger tracing MCP tool server
  protocol: SSE
  url: http://<mgmt-node-ip>:9954/jaeger/sse
  timeout: 60s
  sseReadTimeout: 1h
---
apiVersion: kagent.dev/v1alpha2
kind: RemoteMCPServer
metadata:
  name: sregym-submit
  namespace: kagent
spec:
  description: SREGym conductor submit MCP tool (diagnosis/mitigation submission)
  protocol: SSE
  url: http://<mgmt-node-ip>:8000/submit_mcp/sse
  timeout: 60s
  sseReadTimeout: 1h
EOF
```

Replace `<mgmt-node-ip>` with the management cluster control-plane node's IP (in our setup,
`sre-control` = `192.168.28.158`) — SREGym's `MCPServer`/conductor forward these ports to
`0.0.0.0` on that node while a run is active.

**Expected status when no SREGym run is active:** `kubectl get remotemcpservers -n kagent` will
show `ACCEPTED=False` / "connection refused" for all four — this is normal (nothing is listening
yet) and does not block the `Agent` CRD from being `Accepted=True`. It resolves itself the moment
a benchmark run starts the port-forwards. If instead you see a *different* error (e.g.
"network unreachable" rather than "connection refused"), that indicates an actual routing problem
between the management cluster's pod network and the node IP — worth checking before going
further.

## 4. `ModelConfig` + `Agent` CRD

```bash
export KUBECONFIG=/home/ubuntu/mgmt.kubeconfig
cat <<'EOF' | kubectl apply -f -
apiVersion: kagent.dev/v1alpha2
kind: ModelConfig
metadata:
  name: sre-agent-model
  namespace: kagent
spec:
  provider: OpenAI
  model: gpt-5-mini
  apiKeySecret: kagent-openai
  apiKeySecretKey: OPENAI_API_KEY
---
apiVersion: kagent.dev/v1alpha2
kind: Agent
metadata:
  name: sre-agent
  namespace: kagent
spec:
  type: Declarative
  description: SRE agent that diagnoses and mitigates faults in Kubernetes applications benchmarked by SREGym.
  declarative:
    modelConfig: sre-agent-model
    runtime: python
    systemMessage: |
      You are an SRE agent tasked with diagnosing and fixing issues in a Kubernetes application.

      CRITICAL: You are running in an AUTOMATED environment. Work autonomously and make all
      decisions yourself. DO NOT ask for user confirmation or approval.

      WORKFLOW: DIAGNOSIS then MITIGATION.
      - DIAGNOSIS: investigate with the kubectl/prometheus/jaeger tools, then call the `submit`
        tool with a natural language description of the issue found.
      - MITIGATION: fix the root cause using exec_kubectl_cmd_safely, then call `submit` with an
        empty string ("") to trigger validation. This submission is REQUIRED.

      Use the `submit` tool directly — do not attempt to curl or call any HTTP endpoint yourself.
    tools:
    - type: McpServer
      mcpServer:
        apiGroup: kagent.dev
        kind: RemoteMCPServer
        name: sregym-kubectl
        toolNames: [exec_kubectl_cmd_safely, rollback_command, get_previous_rollbackable_cmd]
    - type: McpServer
      mcpServer:
        apiGroup: kagent.dev
        kind: RemoteMCPServer
        name: sregym-prometheus
        toolNames: [get_metrics, get_alerts]
    - type: McpServer
      mcpServer:
        apiGroup: kagent.dev
        kind: RemoteMCPServer
        name: sregym-jaeger
        toolNames: [get_services, get_operations, get_traces, get_dependency_graph]
    - type: McpServer
      mcpServer:
        apiGroup: kagent.dev
        kind: RemoteMCPServer
        name: sregym-submit
        toolNames: [submit]
EOF
```

Notes on the system prompt: it deliberately tells the model to call the `submit` tool directly
(kagent exposes it as a normal MCP tool call), unlike the CLI-based drivers (`codex`,
`claudecode`) whose prompt tells the model to `curl POST /submit` — because those agents have a
generic shell tool and no native MCP tool access, whereas `sre-agent` has `submit` as a first-class
tool via the `RemoteMCPServer` wiring above.

Useful commands while iterating on CRD fields (schemas can shift between kagent versions — verify
rather than assume):

```bash
kubectl explain agents.kagent.dev.spec.declarative
kubectl explain agents.kagent.dev.spec.declarative.tools.mcpServer
kubectl explain remotemcpservers.kagent.dev.spec
kubectl explain modelconfigs.kagent.dev.spec.openAI
kubectl get agent k8s-agent -n kagent -o yaml   # a real bundled example, before disabling it
```

## 5. SREGym-side driver

Two new files, following the existing driver conventions in `clients/*/driver.py`:

- **[`sregym/service/kagent_gateway.py`](../sregym/service/kagent_gateway.py)** — a small class
  mirroring `sregym/service/mcp_server.py:MCPServer`'s port-forward/health-check/retry pattern,
  but for `kagent-controller` on the *management* cluster. It uses its own kubeconfig
  (`KAGENT_KUBECONFIG` env var, default `~/mgmt.kubeconfig`) on every `kubectl` invocation rather
  than the process-wide `KUBECONFIG`, which must stay pointed at the workload cluster.

- **[`clients/kagent/driver.py`](../clients/kagent/driver.py)** — the entry point registered in
  `agents.yaml`. Flow:
  1. `resolve_problem_id()` (same harness helper every driver uses).
  2. `KagentGateway().ensure_started()` — port-forward `kagent-controller:8083` to
     `127.0.0.1`.
  3. Poll conductor `GET /status` until stage ∈ `{diagnosis, mitigation}` (same pattern as
     `clients/codex/driver.py`).
  4. `GET /get_app` for app name/namespace/description, build a short task instruction from it.
  5. Shell out once: `kagent invoke --agent <name> -n kagent --kagent-url http://127.0.0.1:<port>
     --task "<instruction>" --timeout <n>s --output-format json`. This single synchronous call
     covers the *entire* diagnosis+mitigation workflow — the agent submits for both stages itself
     via its `submit` tool, the same way a single `codex`/`claudecode` invocation does via curl.
  6. Save the raw JSON response under `AGENT_LOGS_DIR` (best-effort, for debugging parity with
     other clients).
  7. Poll conductor `GET /status` again until `"done"` — this is the authoritative completion
     signal, independent of the `kagent invoke` call returning, because `/submit` evaluates
     asynchronously server-side and there can be a short tail after the agent's last tool call.
  8. Exit 0 if the conductor reached `"done"`, non-zero otherwise.

Registered in `agents.yaml` with `container_isolation: false` (like `tierzero`/`demo`) — it needs
to reach the management cluster's forwarded port directly as a host subprocess, not from inside
SREGym's shared Docker image/network.

```yaml
  - name: kagent
    kickoff_command: python -m clients.kagent.driver
    kickoff_workdir: .
    kickoff_env:
      KAGENT_KUBECONFIG: /home/ubuntu/mgmt.kubeconfig
      KAGENT_AGENT_NAME: sre-agent
      KAGENT_NAMESPACE: kagent
    install_script: null
    agent_version: null
    container_isolation: false
```

## 6. Running it

```bash
cd ~/SREGym
uv run python main.py --agent kagent --problem k8s_target_port-misconfig --model gpt-5-mini
```

(`--model` here only affects SREGym's own judge model — `sre-agent`'s own LLM calls are entirely
governed by its `ModelConfig` CRD on the management cluster, independent of this flag.)

Two host-side credential quirks worth knowing about with kagent runs specifically: `sre-agent`'s
own OpenAI key lives entirely in a k8s Secret on the management cluster and needs nothing from the
host shell, but the *judge* model (which grades the submission) still runs on the host and still
needs `OPENAI_API_KEY` in whatever shell launches `main.py`. If that's inconvenient to guarantee
(e.g. running from a tool/cron context with a different environment than your interactive shell),
`main.py` now supports `--env-file path/to/.env` (loaded before anything else, default `.env` in
the cwd) and `--skip-judge-preflight` (skips the early sanity ping if you're confident the judge
credential is fine — scoring still fails loudly later if it genuinely isn't).

## 7. Adding another KAgent agent later

`clients/kagent/driver.py` never hardcodes `sre-agent` — it reads the target agent name from the
`KAGENT_AGENT_NAME` env var (agents.yaml's `kickoff_env` always wins over the host environment,
see `sregym/agent_launcher.py:71-73`). So plugging in a *different* KAgent `Agent` CRD — to
compare prompts/models/tool sets side by side — never requires touching Python code again:

1. Deploy the new `Agent` CRD on the management cluster (namespace `kagent`), giving it a
   `submit` tool via the existing `sregym-submit` `RemoteMCPServer` (and `sregym-kubectl` /
   `sregym-prometheus` / `sregym-jaeger` if it needs to act on/observe the cluster) — reuse the
   ones already registered in Section 3, no need to create new ones per agent.
2. Add one more block to `agents.yaml`, copying the `kagent` entry and changing two fields:
   ```yaml
     - name: kagent-<new-agent-name>
       kickoff_command: python -m clients.kagent.driver
       kickoff_workdir: .
       kickoff_env:
         KAGENT_KUBECONFIG: /home/ubuntu/mgmt.kubeconfig
         KAGENT_AGENT_NAME: <new-agent-name>
         KAGENT_NAMESPACE: kagent
       install_script: null
       agent_version: null
       container_isolation: false
   ```
3. Run it: `uv run python main.py --agent kagent-<new-agent-name> --problem <problem_id>`.

All existing `kagent-*` entries keep working independently, so multiple KAgent agents can be
benchmarked side by side.

## 8. Verified result

First end-to-end run (2026-07-27), problem `k8s_target_port-misconfig`, `sre-agent` on gpt-5-mini:

| | `stratus` baseline (2026-07-16) | `kagent` / `sre-agent` (2026-07-27) |
|---|---|---|
| Diagnosis | 100/100, TTL 106s | 100/100, TTL 91.2s |
| Mitigation | Pass, TTM 338s | Pass, TTM 94.6s |

Results at `SREGym/results/0727_0736/kagent/`.

## 9. Known gaps

- ~~No ATIF trajectory conversion for kagent runs~~ **Closed (2026-07-27).**
  `atif_converter/adapters/kagent.py` now converts the raw `kagent invoke` JSON into a full ATIF
  trajectory (pairing each `function_call`/`function_response` history item into one Step with
  `tool_calls`+`observation`, and per-turn `metrics` from `kagent_usage_metadata`), registered in
  `atif_converter/converter.py:SUPPORTED_AGENTS` and dispatched from
  `sregym/traces/convert.py:_resolve_adapter_tool()` for the whole `kagent`/`kagent-*` family (so
  any future `kagent-<name>` entry in `agents.yaml` gets ATIF support automatically, no extra
  registration needed). `main.py` now also merges `Metrics.total_steps` /
  `Metrics.total_tool_calls` / `Metrics.tool_call_breakdown` / `Metrics.total_prompt_tokens` /
  `Metrics.total_completion_tokens` / `Metrics.total_cached_tokens` / `Metrics.total_cost_usd`
  into every run's results-CSV row from whichever trajectory got produced — this is agent-agnostic
  and applies equally to `stratus`/`codex`/`claudecode`/`gemini`/`opencode`, not just `kagent`.
  Two things remain genuinely unavailable rather than merely unimplemented:
  - **No cached/prefill token accounting.** kagent normalizes usage into a Gemini-shaped schema
    (`candidatesTokenCount`/`promptTokenCount`/`totalTokenCount`) regardless of the underlying
    provider, and never surfaces a cache-hit field — even though the real provider (e.g. OpenAI)
    likely reports one internally. `Metrics.total_cached_tokens` is always `null` for kagent runs.
  - **No per-tool-call latency/overhead.** The synchronous `kagent invoke` response has no
    per-turn timestamps (only one top-level `status.timestamp`), so tool-call duration isn't
    computable from it. Would need `kagent invoke --stream` with client-side timestamping, or
    server-side timing added to SREGym's own MCP tool servers.
  - `demo`/`tierzero`/`autosubmit` still have no ATIF adapter at all (unrelated to kagent) — their
    runs still get no `Metrics.*` columns.
- **Session scoping on the kubectl MCP tool is best-effort.** SREGym's kubectl MCP server keys
  its per-run tool state off a `sregym_ssid` header (falls back to a shared `None` key if
  absent — see `mcp_server/kubectl_mcp_tools.py:extract_session_id`); the current `sre-agent`
  Agent CRD doesn't set this header, which is fine for one problem run at a time but would need
  a per-invocation header (via kagent's `tools[].headersFrom`) if concurrent kagent runs against
  the same tool server become a requirement.
