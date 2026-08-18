# SREGym Lab Docs

Local operational documentation for this deployment (not part of upstream SREGym).

- [`deployment.md`](./deployment.md) — step-by-step guide to installing and running SREGym on a
  real (non-kind) cluster topology: one management cluster driving one or more workload/target
  clusters via kubeconfig, plus every local patch that was needed to make it work.
- [`kagent-integration.md`](./kagent-integration.md) — how [KAgent](https://kagent.dev) agents are
  installed on the management cluster and plugged into SREGym as additional `--agent` options,
  reusing SREGym's own MCP tool servers instead of a bespoke tool integration.
- [`parallel-runner-guide.md`](./parallel-runner-guide.md) — running SREGym as Kubernetes Jobs on
  the management cluster (one per workload cluster) instead of a single host process, using
  KAgent agents: what runs where, the RemoteMCPServer fix this requires, and a step-by-step guide
  to running one episode.
