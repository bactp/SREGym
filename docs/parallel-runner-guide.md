# Parallel K8s-based Runner — Architecture and Step-by-Step

> How SREGym's benchmark runner moved from "one `python main.py` process per
> host" to "one Kubernetes Job per workload cluster on the management
> cluster," so multiple scenarios can eventually run concurrently. Assumes
> `docs/deployment.md` (base SREGym on a real cluster topology) and
> `docs/kagent-integration.md` (KAgent agents wired to SREGym's MCP tool
> servers) are already working. Written 2026-08-17 from the session that
> built and verified this.

## 1. What runs on which cluster

```
┌──────────────────────────────────────────────────────────────────────────┐
│ MANAGEMENT CLUSTER (sre-control + sre-worker01 + sre-worker02)           │
│                                                                            │
│  namespace kagent          → kagent-controller, Agent CRDs (sre-agent,   │
│                               sre2, orchestrator, ...), RemoteMCPServer   │
│                               CRDs (sregym-kubectl/prometheus/jaeger/    │
│                               submit) — installed once, per                │
│                               docs/kagent-integration.md, UNCHANGED here  │
│                                                                            │
│  namespace sregym-runner   → everything new, deploy/runner/k8s/          │
│   ├── Secret target-kubeconfig-cluster-a/b/c (one per workload cluster)  │
│   ├── Secret llm-credentials (judge model's OPENAI_API_KEY etc.)         │
│   ├── PVC sregym-results (Longhorn RWX — where trajectories land)        │
│   ├── Service sregym-runner  → stable DNS name for whichever runner Pod │
│   │      is currently active (see §3 below — this is the piece that     │
│   │      replaces the old hardcoded 192.168.28.158 node IP)             │
│   └── Job sregym-runner-cluster-a (or -b / -c)                          │
│         └── 1 Pod, 1 container `runner`:                                 │
│              runs `python main.py --agent kagent --target-kubeconfig    │
│              /secrets/target/kubeconfig ...` — the exact same conductor  │
│              logic as the old bare-host workflow, just inside a Pod      │
└──────────────────────────────────────────────────────────────────────────┘
                    │
                    │  runner calls kubectl/helm using the Secret's kubeconfig
                    ▼  (this part is 100% unchanged from the bare-host flow)
┌──────────────────────────────────────────────────────────────────────────┐
│ WORKLOAD CLUSTER (e.g. sre-test1, one per Secret above)                  │
│  namespace sregym     → MCP tool server (kubectl/prometheus/jaeger/submit)│
│  namespace observe    → Prometheus/Jaeger/Loki/OTel                      │
│  namespace <app>      → benchmark app (social-network, ...) + workload   │
│  + the injected fault, living in one of the above                       │
└──────────────────────────────────────────────────────────────────────────┘
```

No agent Docker container and no `dind` sidecar are involved: KAgent agents
(`agents.yaml` entries with `container_isolation: false`) never spawn a local
Docker container — the LLM agent runs as a pre-existing `Agent` CRD on the
**management cluster itself** (namespace `kagent`), and `clients/kagent/
driver.py` just shells out to `kagent invoke` and polls the conductor's own
`/status`, both inside the same `runner` container on plain `localhost`. (An
earlier iteration of this design targeted the `stratus` agent instead, which
*does* spawn a Docker container and needed a `dind` sidecar for that — see
git history of `deploy/runner/k8s/job-base/job.yaml` if you ever need that
path again.)

## 2. What's actually new vs. the bare-host flow

| | Bare-host (`docs/deployment.md`) | K8s Job (this doc) |
|---|---|---|
| `python main.py` runs | Directly on `sre-control` | Inside the `runner` container of a Pod (any mgmt-cluster node) |
| Conductor API (`:8000`) / MCP port-forward (`:9954`) bind to | `sre-control`'s real network interface (`192.168.28.158`) | The Pod's own cluster-internal IP — **different every time**, and not the node's IP |
| How KAgent's Agent CRD reaches those | Hardcoded `RemoteMCPServer` CRD URLs pointing at `192.168.28.158:8000`/`:9954` | The same CRDs, **repointed once** at `sregym-runner.sregym-runner.svc.cluster.local` (see §3) |
| Target workload cluster kubeconfig | `--target-kubeconfig <path>` on the CLI | Same flag, fed from a mounted Secret instead of a bare file |
| Results | `results/` on `sre-control`'s local disk | PVC `sregym-results`, one subdirectory per Job |

## 3. Required one-time fix: repoint the `RemoteMCPServer` CRDs

**This step is not optional — skip it and the KAgent agent will fail every
tool call with "connection refused."** `docs/kagent-integration.md` §3
installed four `RemoteMCPServer` CRDs pointing at
`http://192.168.28.158:9954/...` and `http://192.168.28.158:8000/...` — that
IP only ever worked because `main.py` used to bind those ports directly on
`sre-control`'s own network interface. A Pod doesn't own the node's
interface, so those fixed-IP URLs no longer reach anything once `main.py`
runs inside a Job.

`deploy/runner/k8s/service.yaml` adds a `Service` (`sregym-runner`, applied
once via the shared `kubectl apply -k deploy/runner/k8s/` — see §4) that
forwards ports `8000`/`9954` to whichever runner Pod is currently running,
addressable at the stable in-cluster DNS name
`sregym-runner.sregym-runner.svc.cluster.local`. Patch the four existing CRDs
to use it instead of the IP:

```bash
export KUBECONFIG=/home/ubuntu/mgmt.kubeconfig
for name in sregym-kubectl sregym-prometheus sregym-jaeger; do
  kubectl patch remotemcpserver "$name" -n kagent --type merge \
    -p '{"spec":{"url":"http://sregym-runner.sregym-runner.svc.cluster.local:9954/'"${name#sregym-}"'/sse"}}'
done
kubectl patch remotemcpserver sregym-submit -n kagent --type merge \
  -p '{"spec":{"url":"http://sregym-runner.sregym-runner.svc.cluster.local:8000/submit_mcp/sse"}}'
```

**Known limitation (not yet solved): this only supports ONE active Job at a
time.** The `sregym-runner` Service's selector matches every runner Pod
regardless of which cluster overlay created it, and all three KAgent agents
(`kagent`/`kagent-sre2`/`kagent-orchestrator`) share these same four
`RemoteMCPServer` CRDs. Running `cluster-a` and `cluster-b` Jobs at the same
time would load-balance tool calls randomly between both Pods' MCP servers —
silently corrupting both runs. True 3-way parallelism needs **one dedicated
set of 4 `RemoteMCPServer` CRDs + one uniquely-labeled Service per KAgent
agent** (e.g. `sregym-kubectl-cluster-a` → `Service` selecting only
`job-name: sregym-runner-cluster-a` pods, wired into the `kagent`-named
Agent's tool list; same pattern × 3). Until that's built, run one cluster's
Job to completion before starting the next.

## 4. Step-by-step: running one episode

1. **One-time cluster setup** (skip if already done):
   ```bash
   docker build -f deploy/runner/Dockerfile -t <your-registry>/sregym-runner:latest .
   docker push <your-registry>/sregym-runner:latest
   # update newName/newTag under images: in deploy/runner/k8s/kustomization.yaml
   # and deploy/runner/k8s/overlays/cluster-*/kustomization.yaml to match

   kubectl apply -k deploy/runner/k8s/          # namespace, SA, StorageClass, PVC, Service

   kubectl create secret generic target-kubeconfig-cluster-a \
     --from-file=kubeconfig=<path-to-cluster-a.kubeconfig> -n sregym-runner
   kubectl create secret generic llm-credentials \
     --from-literal=OPENAI_API_KEY=<your-key> -n sregym-runner

   # §3 above — one-time, only needs redoing if the RemoteMCPServer CRDs are ever reset
   ```

2. **Pick a scenario.** Edit `deploy/runner/k8s/overlays/cluster-a/patch.yaml`:
   ```yaml
   - name: PROBLEM_ID
     value: target_port          # any problem id from sregym/conductor/problems/registry.py
   - name: AGENT_NAME
     value: kagent                # kagent | kagent-sre2 | kagent-orchestrator | kagent-ctx-skill | kagent-orchestrator-ctx-skill
   - name: N_ATTEMPTS
     value: "1"
   ```
   See [`docs/problem-catalog.md`](./problem-catalog.md) for the full list of problem IDs with fault type, target app, and whether the fault is injected at the app or infrastructure layer.

   Also confirm the `secretName` in that same file matches the Secret from step 1.

3. **Run it:**
   ```bash
   kubectl apply -k deploy/runner/k8s/overlays/cluster-a/
   kubectl get pods -n sregym-runner -w
   ```

4. **Watch it work:**
   ```bash
   kubectl logs -f job/sregym-runner-cluster-a -n sregym-runner
   ```
   Expect, in order: image pull (slow the first time on a given node, ~2GB
   image) → `SREGym-applications` copied into `/workspace` → `main.py`
   startup, judge pre-flight check → deploy Prometheus/Jaeger/OpenEBS/MCP
   server + benchmark app onto the target workload cluster → inject fault →
   `clients/kagent/driver.py` calls `kagent invoke` → agent diagnoses/
   mitigates via the MCP tools (now reachable per §3) → oracle scores it →
   teardown/reconcile.

5. **Confirm completion:**
   ```bash
   kubectl get job sregym-runner-cluster-a -n sregym-runner
   ```
   A `Failed` condition with `reason: BackoffLimitExceeded` after the run
   means `main.py` exited non-zero (check the logs above for why — a missing
   `OPENAI_API_KEY` is the most common cause). A Pod stuck `Running`
   indefinitely past when the logs show completion means something didn't
   exit cleanly — check `kubectl describe pod ...` for container statuses.

6. **Find the results** (persisted on the PVC, survives Pod deletion):
   ```bash
   # from any pod/debug-pod with the PVC mounted at its root (no subPathExpr):
   ls /pvc-root/sregym-runner-cluster-a/results/<timestamp>/<agent>/<problem>/run_1/
   cat .../run_1/trajectory.json
   ```

7. **Clean up** (optional — Jobs aren't auto-deleted):
   ```bash
   kubectl delete job sregym-runner-cluster-a -n sregym-runner
   ```

8. **Repeat for `cluster-b`/`cluster-c`** — but per §3's known limitation,
   **wait for the previous Job to finish first** until the per-agent
   `RemoteMCPServer` fix is built.

## 5. Aggregating results (once you have more than one run)

```bash
python -m sregym.traces.postprocess /pvc-root/
python -m sregym.traces.store ingest /pvc-root/ --db /pvc-root/traces-merged.db
python -m sregym.traces.store stats --db /pvc-root/traces-merged.db
```

## 6. Files touched by this design

| File | Purpose |
|---|---|
| `deploy/runner/Dockerfile` | Runner image (kubectl + helm + docker CLI + Python deps + `SREGym-applications/` + `docker/agents/` baked in) |
| `deploy/runner/k8s/{namespace,serviceaccount,storageclass,pvc,service}.yaml` | Shared, cluster-independent resources — applied once |
| `deploy/runner/k8s/job-base/job.yaml` | The Job template (single `runner` container, no `dind`) |
| `deploy/runner/k8s/overlays/cluster-{a,b,c}/` | Per-cluster Job name, target kubeconfig Secret, and scenario (`PROBLEM_ID`/`AGENT_NAME`/`N_ATTEMPTS`) |
| `clients/kagent/driver.py` | Added `KAGENT_CONTROLLER_URL` env override to skip `kubectl port-forward` to kagent-controller (the Pod is already on that cluster, so in-cluster DNS reaches it directly) |
| `deploy/runner/README.md` | Shorter operator quick-reference; this file has the full picture |
