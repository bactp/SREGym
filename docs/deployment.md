# SREGym Deployment Guide — Step-by-Step Reproduction

> Complete record of how SREGym was installed, configured, patched, and validated in our lab
> (2026-07-16, updated 2026-07-27). Written because the upstream README is not detailed enough
> to reproduce a working setup on a real (non-kind) cluster with an existing platform stack.
>
> See [`docs/kagent-integration.md`](./kagent-integration.md) for how KAgent-hosted agents are
> plugged into this same setup as an additional `--agent` option.

## Topology: one management cluster, N workload clusters

This deployment follows a **hub-and-spoke pattern**: a single management cluster hosts the
long-running control plane (SREGym itself, and separately KAgent — see the companion doc), and
each workload/target cluster is a disposable, fault-injectable spoke that SREGym drives entirely
through a kubeconfig. Nothing SREGym-specific runs *inside* a workload cluster except what a
benchmark run deploys and tears down again (the app under test, Prometheus/Jaeger/Loki, the MCP
tool server) — the workload cluster's own pre-existing platform stack (GitOps controllers,
CNI, storage) is left alone.

- **Management cluster:** `sre-control` — in our case a real 3-node kubeadm cluster
  (control-plane `sre-control` + 2 workers), already running Cluster API, Flux, ArgoCD,
  cert-manager, MetalLB, Longhorn, Gitea, etc. as its own platform stack. SREGym itself runs here
  as a **host process** (not a Kubernetes workload) on the control-plane node, driven via `uv run
  python main.py`.
- **Workload/target cluster(s):** e.g. `sre-test1` — Kubernetes v1.32.8 (CAPI-provisioned), 1
  control-plane + 2 workers, with its own pre-existing platform stack: **Longhorn (default
  StorageClass), ArgoCD, Flux, MetalLB, flannel**. This is where faults get injected and the
  benchmark application gets deployed each run.
- **Which cluster SREGym targets is purely a matter of `$KUBECONFIG`/`--target-kubeconfig`** — see
  Section 3 below. `~/.kube/config` on the runner host is **reserved for this host's own default
  cluster (the management cluster)** and must never be overwritten with a workload cluster's
  kubeconfig — SREGym never reads it as a fallback, by design (see Section 3). A separate,
  dedicated kubeconfig (e.g. `~/mgmt.kubeconfig`, a copy of `/etc/kubernetes/admin.conf`) is used
  for anything that needs to talk to the *management* cluster from SREGym-adjacent tooling
  (KAgent, etc.). Adding another workload cluster later means: bring up the cluster, drop its
  admin kubeconfig somewhere on the runner (e.g. `~/clusters/<name>.kubeconfig`), and pass
  `--target-kubeconfig ~/clusters/<name>.kubeconfig` (or export `KUBECONFIG` to that path) for
  that run — no other SREGym config changes needed, and no risk of clobbering another cluster's
  kubeconfig.
- Node name → IP mapping (`sre-test1`):
  | K8s node name | IP |
  |---|---|
  | `sre-test1-control-plane-x77z8` | 192.168.28.184 |
  | `sre-test1-md-0-n2wps-fwjk8` | 192.168.28.202 |
  | `sre-test1-md-0-n2wps-zxtrt` | 192.168.28.191 |
- Credentials on the runner: `~/sre-test1.kubeconfig` (admin kubeconfig for the workload
  cluster), `~/mgmt.kubeconfig` (admin kubeconfig for the management cluster, copied from
  `/etc/kubernetes/admin.conf`), `~/workflow-prj.pem` (RSA-2048 SSH key for all workload-cluster
  nodes, user `ubuntu`, passwordless sudo).

---

## 0. Fix credential file permissions (if needed)

Our kubeconfig was root-owned (scp'd as root), which breaks everything running as `ubuntu`:

```bash
sudo chown ubuntu:ubuntu ~/sre-test1.kubeconfig
chmod 600 ~/workflow-prj.pem

# sanity check
kubectl get nodes -o wide --kubeconfig ~/sre-test1.kubeconfig
```

## 1. Install host prerequisites

Required on the runner: **Python 3.12+ (via uv), Docker, kubectl, Helm, uv, git**.

```bash
# uv (also manages Python 3.12 for the project — system Python 3.10 is fine)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"        # add to ~/.bashrc too

# Helm (official binary + checksum verification)
curl -fsSLO https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz
curl -fsSLO https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz.sha256sum
sha256sum -c helm-v3.19.0-linux-amd64.tar.gz.sha256sum
tar -xzf helm-v3.19.0-linux-amd64.tar.gz
sudo mv linux-amd64/helm /usr/local/bin/helm

# Docker: must run WITHOUT sudo (agent containers are launched by the framework)
docker ps            # if this fails: sudo usermod -aG docker $USER && re-login
```

## 2. Clone SREGym and install dependencies

```bash
git clone --recurse-submodules https://github.com/SREGym/SREGym ~/SREGym
cd ~/SREGym

# If you cloned without --recurse-submodules, the benchmark apps are MISSING.
# Symptom: `git submodule status` shows a leading "-". Fix:
git submodule update --init --recursive

uv sync              # creates .venv with Python 3.12 + all deps
uv run python main.py --help    # smoke test
```

## 3. Kubeconfig placement

**SREGym requires an explicit target-cluster kubeconfig — it never falls back to
`~/.kube/config`** (fixed 2026-07-28, `sregym/service/target_cluster.py:resolve_target_kubeconfig()`,
used by every call site that used to hardcode `config.load_kube_config()` with no arguments —
those silently ignored `$KUBECONFIG` before this fix). `~/.kube/config` on this host is reserved
for the management cluster's own default and is never touched by SREGym.

Point SREGym at a workload cluster one of two ways — either works, pick whichever fits your
workflow:

```bash
# Option A: pass it on the command line every run
uv run python main.py --target-kubeconfig ~/sre-test1.kubeconfig --agent stratus --problem ...

# Option B: export it once per shell session
export KUBECONFIG=~/sre-test1.kubeconfig
uv run python main.py --agent stratus --problem ...
```

`main.py` fails fast with a clear error at startup if neither is set, or if the path doesn't
exist — it will not silently run against whatever `~/.kube/config` happens to contain.

> Everything SREGym deploys/injects goes to whichever kubeconfig was resolved above. If you juggle
> multiple workload clusters, keep one kubeconfig file per cluster (e.g. `~/clusters/sre-test1.kubeconfig`,
> `~/clusters/sre-test2.kubeconfig`) and pick the right one per run — never overwrite one cluster's
> file with another's. Note: a few oracle-evaluation code paths
> (`clients/stratus/weak_oracles/{workload_oracle,cluster_state_oracle}.py`) *intentionally* bypass
> the agent-facing filtering proxy and read the real target cluster directly (still via the same
> resolved kubeconfig, just without the namespace/label filtering) — they need the unfiltered
> ground truth. That's by design, not a bug.

## 4. SSH setup for OS-level fault injection

Only needed for node-level problems (kubelet crash, disk pressure, …) — but set it up once.

### 4.1 /etc/hosts mapping — ⚠️ critical correctness detail

SREGym maps Kubernetes node names to Ansible inventory hosts **by substring matching**
(`sregym/generators/fault/inject_remote_os.py:151`: `if node_name in host or host in node_name`).
If `ansible_host` is a bare IP, the match never succeeds and it **silently falls back to the
first worker** — OS faults would hit the wrong node. Solution: use the node *names* as
`ansible_host` and make them resolvable:

```bash
sudo tee -a /etc/hosts <<'EOF'
192.168.28.184 sre-test1-control-plane-x77z8
192.168.28.202 sre-test1-md-0-n2wps-fwjk8
192.168.28.191 sre-test1-md-0-n2wps-zxtrt
EOF
```

### 4.2 Ansible inventory

Create `scripts/ansible/inventory.yml` (from `inventory.yml.example`):

```yaml
all:
  vars:
    k8s_user: ubuntu
    ansible_ssh_private_key_file: /home/ubuntu/workflow-prj.pem
    ansible_ssh_common_args: '-o StrictHostKeyChecking=no'
  children:
    control_nodes:
      hosts:
        sre-test1-control-plane-x77z8:
          ansible_host: sre-test1-control-plane-x77z8
          ansible_user: "{{ k8s_user }}"
    worker_nodes:
      hosts:
        sre-test1-md-0-n2wps-fwjk8:
          ansible_host: sre-test1-md-0-n2wps-fwjk8
          ansible_user: "{{ k8s_user }}"
        sre-test1-md-0-n2wps-zxtrt:
          ansible_host: sre-test1-md-0-n2wps-zxtrt
          ansible_user: "{{ k8s_user }}"
```

### 4.3 paramiko key discovery — ⚠️ non-obvious

The OS-fault injector uses **paramiko with no explicit key path**
(`inject_remote_os.py:95-105`: `ssh.connect(host, username=user)`), so it only finds keys via
**ssh-agent** or the default `~/.ssh/id_rsa`. It does NOT read `~/.ssh/config` or the
inventory's `ansible_ssh_private_key_file`. Pick one:

```bash
# Option A (persistent):
cp ~/workflow-prj.pem ~/.ssh/id_rsa && chmod 600 ~/.ssh/id_rsa

# Option B (per session, before running main.py):
eval $(ssh-agent -s) && ssh-add ~/workflow-prj.pem
```

### 4.4 Pre-populate known_hosts and verify

```bash
ssh-keyscan -T 5 sre-test1-control-plane-x77z8 sre-test1-md-0-n2wps-fwjk8 \
  sre-test1-md-0-n2wps-zxtrt >> ~/.ssh/known_hosts

# every node must print its hostname AND pass passwordless sudo:
for h in sre-test1-md-0-n2wps-fwjk8 sre-test1-md-0-n2wps-zxtrt sre-test1-control-plane-x77z8; do
  ssh -i ~/workflow-prj.pem -o BatchMode=yes ubuntu@$h 'hostname && sudo -n true && echo SUDO_OK'
done
```

## 5. Prepare the target cluster

### 5.1 Remove the stale cloud-provider taint — ⚠️ blocks everything

Our CAPI control-plane node carried `node.cloudprovider.kubernetes.io/uninitialized=true:NoSchedule`
(kubelet started with `--cloud-provider=external` but **no cloud-controller-manager exists** in
the cluster, so nothing ever clears it). SREGym pins its entire observability stack
(Prometheus, Alertmanager, Jaeger, Loki, OTel, MCP server, Khaos) to the control-plane node —
with this taint present, every deploy hangs for 600 s and fails.

SREGym's own reference setup (`scripts/ansible/setup_cluster.yml:636`) untaints the
control-plane entirely, so we did the same:

```bash
kubectl taint node sre-test1-control-plane-x77z8 node.cloudprovider.kubernetes.io/uninitialized-
kubectl taint node sre-test1-control-plane-x77z8 node-role.kubernetes.io/control-plane:NoSchedule-
```

Side effect to expect: Longhorn's DaemonSets (manager, csi-plugin, instance-manager,
engine-image) will roll onto the control-plane node in the following minutes — this is
required, otherwise PVC attach on that node fails with
`CSINode <node> does not contain driver driver.longhorn.io`. The instance-manager image is
large (~300 MB); wait for it once.

### 5.2 Understand what SREGym will install

On the first run SREGym installs into the target cluster: metrics-server, OpenEBS
(+ patches default StorageClass — see patch #2 below), Prometheus/Alertmanager/kube-state-metrics/
blackbox/pushgateway (namespace `observe`), Jaeger, OTel Collector, Loki, the SREGym MCP server
(namespace `sregym`), and per-problem benchmark applications (e.g. namespace `social-network`).
It also captures a **cluster baseline** and, at cleanup, **reconciles the cluster back to that
baseline — including node taints** (see patch #4).

## 6. Local patches applied (8 files, 39 insertions / 3 deletions)

### Patch 1 — missing `fastapi` in the agent container (upstream bug)

**Symptom:** agent preflight fails with `preflight failed: No module named 'fastapi'`.
**File:** `docker/agents/requirements-container.txt` — append:

```
fastapi==0.115.12
```

(version matches the pin in `pyproject.toml`; rebuild with `--force-build` after changing).

### Patch 2 — don't steal the default StorageClass from Longhorn

**Problem:** every deploy unconditionally runs
`kubectl patch storageclass openebs-hostpath ... is-default-class:"true"`
(`sregym/conductor/conductor.py:802-810`), clashing with Longhorn-as-default and any
GitOps-managed workloads relying on it.
**Change:** wrap the patch in a guard — query existing default StorageClasses first and only
promote `openebs-hostpath` when **no default exists**; otherwise log and skip. OpenEBS itself
still installs (some problems need it); benchmark PVCs that don't pin a class simply land on
Longhorn, which works.

### Patch 3 — tolerate the `uninitialized` taint on all control-plane-pinned components

Belt-and-suspenders for clusters where the taint may reappear (see patch 4). Added

```yaml
- key: "node.cloudprovider.kubernetes.io/uninitialized"
  operator: "Exists"
```

next to every existing `node-role.kubernetes.io/control-plane` toleration in:

| File | blocks patched |
|---|---|
| `sregym/observer/prometheus/prometheus/values.yaml` | 5 (server, alertmanager, kube-state-metrics, pushgateway, blackbox) |
| `sregym/observer/jaeger/jaeger.yaml` | 1 |
| `sregym/observer/otel_collector/otel-collector.yaml` | 1 |
| `sregym/observer/loki/loki-values.yaml` | 2 |
| `sregym/service/khaos.yaml` | 1 |
| `mcp_server/k8s/deployment.yaml` | 1 |

### Patch 4 — scrub stale taints from the persisted cluster baseline

**Problem:** the first-ever deploy captured the baseline **while the stale taints were still
present**. SREGym's cleanup calls `_reconcile_node_taints()`
(`sregym/service/cluster_state.py:340`) which restores baseline taints — so the taints we
removed in step 5.1 were **re-applied after every problem run**, breaking the next run.
**Fix:** edit the baseline JSON and empty the control-plane taint list:

```bash
python3 - <<'EOF'
import json
f = "/home/ubuntu/cache_dir/cluster_baseline_state.json"   # sregym/paths.py: CACHE_DIR
d = json.load(open(f))
d["node_taints"]["sre-test1-control-plane-x77z8"] = []
json.dump(d, open(f, "w"), indent=2)
EOF
```

(Alternative: delete the file and let SREGym re-capture a clean baseline **after** step 5.1.)

## 7. API key and environment

The simplest option is still a real environment variable in the shell that launches `main.py`
(LiteLLM reads it):

```bash
echo 'export OPENAI_API_KEY="sk-..."' >> ~/.bashrc && source ~/.bashrc
# (or ANTHROPIC_API_KEY / GEMINI_API_KEY / AGENT_API_BASE for self-hosted models)
```

**`.env` support (added 2026-07-27):** `main.py` now also accepts `--env-file path/to/.env`
(default `.env` in the current directory, silently skipped if it doesn't exist), loaded via
`python-dotenv` *before* anything else reads the environment — including the judge pre-flight
check. Useful when the shell that actually launches `main.py` isn't the same one you normally
export credentials in (a cron job, a different tool/process, etc.):

```bash
echo 'OPENAI_API_KEY=sk-...' > .env && chmod 600 .env
uv run python main.py --agent stratus --problem k8s_target_port-misconfig
```

There's also `--skip-judge-preflight`, for when you're confident the judge credential is fine
(or the agent under test manages its own credentials entirely independently of this host
process — see [`docs/kagent-integration.md`](./kagent-integration.md)) and don't want an
unrelated preflight ping to abort the run early. Scoring still fails loudly later if the judge is
genuinely unreachable — this only removes the *early* fail-fast check, it doesn't make the judge
optional.

## 8. Run the first benchmark

```bash
cd ~/SREGym
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG=~/sre-test1.kubeconfig          # for kubectl/helm subprocesses
export WAIT_FOR_POD_READY_TIMEOUT=1500            # default 600 s is too short for first-time
                                                  # image pulls (~18 microservices); env-configurable
                                                  # in sregym/service/kubectl.py:26

uv run python main.py \
  --agent stratus \
  --model gpt-5-mini \
  --problem k8s_target_port-misconfig \
  --n-attempts 1
```

Notes:
- The problem ID is **`k8s_target_port-misconfig`** (see `sregym/conductor/problems/registry.py:154`),
  not `target_port` as some docs suggest. Full list: `Problem List.md` / registry.py.
- First run builds the `sregym-agent-base` Docker image (~5 min) and pulls all images on the
  cluster — expect 20–30 min total. Subsequent runs: ~10 min.
- Deploy failures retry up to 3× per problem before the run is abandoned.
- `--judge-model` defaults to `--model`; pin a strong judge model for formal experiments.

### Expected timeline of a healthy run (from our logs)

```
✅ Judge pre-flight        (~10 s)
✅ Agent pre-flight        (~15 s)
[DEPLOY] metrics-server, OpenEBS (skips default-SC patch), Prometheus (~90 s warm),
         Jaeger, OTel, Loki, MCP server        (~3.5 min total warm)
[ENV]    Deploy application social-network     (~4 min warm)
[ENV]    Start workload, Inject fault
[STAGE]  diagnosis  → agent runs (ReAct loop, ~10-30 tool calls) → submit
✅ Correct diagnosis (score 100.0/100), TTL ≈ 106 s
[STAGE]  mitigation → agent patches the Service, verifies, restarts upstreams → submit
✅ Mitigation Result: Pass, TTM ≈ 338 s
[CLEANUP] undeploy app, recover fault, reconcile cluster to baseline
✅ Benchmark complete
```

## 9. Where the results live

```
results/<MMDD_HHMM>/
├── stratus_ALL_results.csv                  # one row per attempt: scores, TTL, TTM, judge checklist
└── stratus/<problem_id>/run_1/
    ├── trajectory.json                      # ATIF v1.7: every think/tool-call/observation + tokens
    ├── *_agent_trajectory.jsonl             # raw agent trace
    ├── driver.log / driver.rc               # agent container stdout / exit code
    └── sregym_*.log                         # full framework log for the run
logs/sregym_<MMDD_HHMM>.log                  # live framework log (tail -f this during runs)
results/traces.db                            # SQLite ingest of all trajectories (cross-run queries)
```

## 10. Troubleshooting (symptom → cause → fix)

| Symptom | Cause | Fix |
|---|---|---|
| `preflight failed: No module named 'fastapi'` | container requirements miss fastapi (upstream bug) | Patch 1 + `--force-build` |
| `main.py: error: No target workload-cluster kubeconfig configured` at startup | neither `--target-kubeconfig` nor `$KUBECONFIG` was set | Section 3 |
| `main.py: error: --target-kubeconfig/$KUBECONFIG points at a file that doesn't exist` | path typo, or a stale/removed kubeconfig file | Section 3 |
| `❌ Judge pre-flight check failed` / process exits immediately | `OPENAI_API_KEY` (or equivalent) not present in *this specific* shell's environment | Section 7 — use `--env-file`, or `--skip-judge-preflight` if you're confident it's fine |
| `observe` pods Pending: `untolerated taint {node.cloudprovider.kubernetes.io/uninitialized}` | stale CAPI taint, no CCM to clear it | Section 5.1 (+ Patches 3–4) |
| Alertmanager stuck ContainerCreating: `CSINode ... does not contain driver driver.longhorn.io` | Longhorn CSI never scheduled on control-plane (taint) | untaint, wait for Longhorn DaemonSets to spread |
| Taints reappear after every run | cleanup reconciles node taints from a stale baseline | Patch 4 |
| `Timeout: Not all pods in namespace '...' reached Ready within 600 seconds` | first-time image pulls exceed default timeout | `export WAIT_FOR_POD_READY_TIMEOUT=1500` |
| OS-fault problems hit the wrong node | inventory `ansible_host` is an IP; substring match with node name fails → falls back to first worker | Section 4.1–4.2 (node names as ansible_host + /etc/hosts) |
| paramiko `AuthenticationException` on OS faults | key not in ssh-agent / not at `~/.ssh/id_rsa` | Section 4.3 |
| Agent's compound/piped kubectl commands "rejected" in logs | intentional guardrails (no pipes/`&&`/redirects; dry-run; read-only in diagnosis) | expected behavior, not an error |

## 11. Verified result of this setup

First full run (2026-07-16, agent `stratus`, model `gpt-5-mini`, problem
`k8s_target_port-misconfig`): **Diagnosis 100/100 (TTL 106 s), Mitigation Pass (TTM 338 s)**,
clean teardown, platform stack (Longhorn/ArgoCD/Flux/MetalLB) untouched.
