"""Problem base class"""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger("all.sregym.problem")


class Problem(ABC):
    def __init__(self, app, namespace: str | None = None):
        self.app = app
        self.namespace = app.namespace if namespace is None else namespace
        self.fault_injected = False
        self.results = {}
        self.root_cause = None  # root cause of the problem in natural language

        # Optional: attach oracles in subclass
        self.diagnosis_oracle = None
        self.mitigation_oracle = None

    def requires_khaos(self) -> bool:
        """Override this method to return True if the problem requires Khaos for fault injection."""
        return False

    @classmethod
    def build_structured_root_cause(
        cls,
        *,
        component: str,
        namespace: str,
        description: str,
    ) -> str:
        """Return canonical structured root_cause text for judge-side parsing.

        Format:
        [fault_spec] component=<...>; namespace=<...> || <human-readable-description>
        """
        kv = [("component", component), ("namespace", namespace)]
        meta = "; ".join(f"{k}={str(v).strip()}" for k, v in kv)

        return f"[fault_spec] {meta} || {description.strip()}"

    @abstractmethod
    def inject_fault(self):
        pass

    @abstractmethod
    def recover_fault(self):
        pass

    def confirm_fault_active(self, timeout: float = 90.0) -> bool:
        """Best-effort default: block until this problem's namespace has left its
        transient post-injection churn (pods stuck Pending/ContainerCreating/
        PodInitializing) and settled into some stable, observable state - either
        genuinely healthy, or stably broken (e.g. CrashLoopBackOff - KubeCtl.is_ready
        treats that as "stable", not "healthy", the same semantics already used to
        gate app deployment via wait_for_stable).

        Exists because inject_fault() alone only confirms the injection *call*
        happened (e.g. a webhook was created, a pod was deleted) - it does not wait
        for the fault's actual symptom to become observable. Found live 2026-08-06:
        an agent invoked immediately after inject_fault() returned repeatedly caught
        a just-recreated pod still mid-init-container (a normal ~74s startup step,
        unrelated to the injected fault) and concluded "stuck forever" - a decision-
        latency artifact, not a reasoning failure. This call closes that gap
        generically, for every problem, without needing a per-problem symptom
        taxonomy - override this method for a fault-specific check when this
        best-effort default isn't sharp enough for a given fault type.

        Returns True if the namespace stabilized within timeout, False otherwise
        (logged as a warning either way by the caller) - never raises, so a slow or
        unusual fault can't hang the whole conductor.
        """
        kubectl = getattr(self, "kubectl", None)
        if kubectl is None:
            from sregym.service.kubectl import KubeCtl

            kubectl = KubeCtl()

        try:
            kubectl.wait_for_stable(self.namespace, sleep=5, max_wait=int(timeout))
            return True
        except Exception as e:
            logger.warning(
                f"confirm_fault_active: namespace '{self.namespace}' did not stabilize "
                f"within {timeout}s ({e}) - proceeding anyway, TTL/agent-visible state "
                f"may reflect mid-transition churn rather than a settled incident."
            )
            return False
