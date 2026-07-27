# SREGym Lab Docs

Local operational documentation for this deployment (not part of upstream SREGym).

- [`deployment.md`](./deployment.md) — step-by-step guide to installing and running SREGym on a
  real (non-kind) cluster topology: one management cluster driving one or more workload/target
  clusters via kubeconfig, plus every local patch that was needed to make it work.
- [`kagent-integration.md`](./kagent-integration.md) — how [KAgent](https://kagent.dev) agents are
  installed on the management cluster and plugged into SREGym as additional `--agent` options,
  reusing SREGym's own MCP tool servers instead of a bespoke tool integration.
