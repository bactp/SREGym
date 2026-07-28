"""Single source of truth for which workload/target cluster SREGym talks to.

The `kubernetes` Python client's `config.load_kube_config()` does NOT honor $KUBECONFIG
when called with no arguments - it hardcodes `KUBE_CONFIG_DEFAULT_LOCATION` (~/.kube/config).
Every call site in SREGym that needs the target cluster must therefore resolve the path
explicitly via `resolve_target_kubeconfig()` and pass it as `config_file=`.

~/.kube/config is intentionally never used as a fallback here: on this deployment it is
reserved for whatever cluster this host's own `kubectl` defaults to (the management
cluster), and SREGym must not silently fault-inject/generate load against it just because
a run forgot to specify a target.
"""

import os


class TargetClusterNotConfiguredError(RuntimeError):
    pass


def resolve_target_kubeconfig() -> str:
    """Return the workload/target cluster's kubeconfig path, set via $KUBECONFIG.

    main.py sets $KUBECONFIG explicitly (from --target-kubeconfig or an already-exported
    value) before any of this runs. Raises if unset rather than falling back to
    ~/.kube/config.
    """
    path = os.environ.get("KUBECONFIG")
    if not path:
        raise TargetClusterNotConfiguredError(
            "No target workload-cluster kubeconfig configured ($KUBECONFIG is unset). "
            "Pass --target-kubeconfig <path> to main.py (or export KUBECONFIG yourself) "
            "pointing at the workload cluster to benchmark against. SREGym intentionally "
            "does not fall back to ~/.kube/config, which is reserved for this host's own "
            "default cluster."
        )
    return os.path.expanduser(path)
