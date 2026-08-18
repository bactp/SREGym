# Parallel SREGym runner (Kubernetes Jobs, KAgent agents)

Runs the SREGym conductor (`main.py`) as a Kubernetes Job on the **management**
cluster, one Job per workload cluster, so N scenarios collect ATIF trajectory
data concurrently instead of one sequential host process at a time. Assumes
KAgent-hosted agents (`agents.yaml` entries with `container_isolation: false`
— `kagent`, `kagent-sre2`, `kagent-orchestrator`, etc.), not Stratus. See
`/home/ubuntu/.claude/plans/concurrent-toasting-badger.md` for the original
design rationale (that version targeted Stratus and used a `dind` sidecar —
superseded by this simpler design once we confirmed only KAgent agents are in
use; see `docs/parallel-runner-guide.md` for the full architecture writeup
and a step-by-step guide to running one episode).

**Status: verified end-to-end against a real workload cluster on 2026-08-17**
(under the earlier Stratus/dind design — the KAgent simplification below
removes the parts that needed that verification in the first place: no more
Docker-in-Docker, no more agent-container networking, no more mgmt-cluster
kubeconfig Secret).

## Why this is simpler than a Stratus-based runner

KAgent agents never spawn a local Docker container: the actual LLM agent runs
as a **pre-existing kagent Agent CRD on this same mgmt cluster** (namespace
`kagent`); `clients/kagent/driver.py` just shells out to `kagent invoke` and
polls the conductor's own `/status` — both in the same `runner` container, on
plain `localhost`. So there's no `docker run --network=host`, no bind-mount
path-visibility problem, and no `dind` sidecar needed at all.

The only remaining cross-cluster hop is: `driver.py` needs to reach the
**kagent-controller API** (to issue `kagent invoke`), which lives in the
`kagent` namespace of the *same* mgmt cluster this Job runs on. Rather than
`kubectl port-forward` to it (the default, host-oriented behavior, which
needs a kubeconfig for this cluster and a `kubectl` subprocess), the Job sets
`KAGENT_CONTROLLER_URL=http://kagent-controller.kagent.svc.cluster.local:8083`
— a small opt-in override added to `driver.py` that calls the ClusterIP
Service directly via in-cluster DNS, since the Pod is already on that
cluster. This also means **no mgmt-cluster kubeconfig Secret is needed** —
`agents.yaml`'s `KAGENT_KUBECONFIG: /home/ubuntu/mgmt.kubeconfig` (a host
path that wouldn't exist in the container) is simply never read when the URL
override is set.

## Real-world gotchas (found running the earlier Stratus/dind version — still relevant for storage/image/credentials)

1. **Longhorn RWX needs `nfs-common` on every node** that can schedule this
   Pod (`sudo apt-get install -y nfs-common` — see git history of this file
   or `docs/parallel-runner-guide.md` for the full symptom/fix).
2. **Default StorageClass replica count may not match your node count.**
   `k8s/storageclass.yaml` defines a dedicated `sregym-runner-results` class
   with `numberOfReplicas: 2` for a 2-node Longhorn topology — adjust to your
   real node count.
3. **Check real per-node Longhorn headroom before sizing the PVC** — see
   `docs/parallel-runner-guide.md`. `k8s/pvc.yaml` defaults to 20Gi.
4. **First image pull on each node is slow** (~2GB image) — not a hang.
5. **LLM credentials for the judge model** (`OPENAI_API_KEY` etc. — the judge
   always runs via LiteLLM regardless of agent type) come from the
   `llm-credentials` Secret via `envFrom` (`optional: true`).

## One-time setup

1. **Build and push the runner image** (from the repo root):
   ```bash
   docker build -f deploy/runner/Dockerfile -t <your-registry>/sregym-runner:latest .
   docker push <your-registry>/sregym-runner:latest
   ```
   Update `newName`/`newTag` under `images:` in `k8s/kustomization.yaml` and
   each `overlays/cluster-*/kustomization.yaml` to match.

2. **Apply the shared resources once** (namespace, ServiceAccount,
   StorageClass, PVC):
   ```bash
   kubectl apply -k deploy/runner/k8s/
   ```

3. **Create the kubeconfig Secrets** — one per workload cluster — and the
   LLM credentials Secret:
   ```bash
   kubectl create secret generic target-kubeconfig-cluster-a \
     --from-file=kubeconfig=<path-to-cluster-a.kubeconfig> -n sregym-runner
   kubectl create secret generic target-kubeconfig-cluster-b \
     --from-file=kubeconfig=<path-to-cluster-b.kubeconfig> -n sregym-runner
   kubectl create secret generic target-kubeconfig-cluster-c \
     --from-file=kubeconfig=<path-to-cluster-c.kubeconfig> -n sregym-runner
   kubectl create secret generic llm-credentials \
     --from-literal=OPENAI_API_KEY=<your-key> -n sregym-runner
   ```
   No `mgmt-kubeconfig` Secret needed (see above).

4. **Edit each overlay's `patch.yaml`** (`k8s/overlays/cluster-{a,b,c}/patch.yaml`)
   to set `PROBLEM_ID`/`AGENT_NAME`/`N_ATTEMPTS` for that cluster's scenario,
   and confirm the `secretName` matches step 3. `AGENT_NAME` must be one of
   the KAgent entries in `agents.yaml` (`kagent`, `kagent-sre2`,
   `kagent-orchestrator`, `kagent-ctx-skill`, `kagent-orchestrator-ctx-skill`).

## Running

See `docs/parallel-runner-guide.md` for the full step-by-step (including
which file to edit for a new scenario and what "done" looks like). Quick
version:
```bash
kubectl apply -k deploy/runner/k8s/overlays/cluster-a/
kubectl apply -k deploy/runner/k8s/overlays/cluster-b/
kubectl apply -k deploy/runner/k8s/overlays/cluster-c/
kubectl get jobs -n sregym-runner -w
```

## Aggregating results after a sweep

```bash
python -m sregym.traces.postprocess /pvc-root/
python -m sregym.traces.store ingest /pvc-root/ --db /pvc-root/traces-merged.db
python -m sregym.traces.store stats --db /pvc-root/traces-merged.db
```

## Adding a 4th+ cluster

Copy `k8s/overlays/cluster-a/` to a new `cluster-d/`, update the `nameSuffix`
in `kustomization.yaml` and the `secretName`/env values in `patch.yaml`.
