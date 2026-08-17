import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.sustained_readiness import SustainedReadinessOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class LivenessProbeTooAggressive(Problem):
    def __init__(self, app_name: str = "social_network"):
        self.app_name = app_name
        self.faulty_service = "aux-service"

        if app_name == "social_network":
            app = SocialNetwork()
        elif app_name == "hotel_reservation":
            app = HotelReservation()
        elif app_name == "astronomy_shop":
            app = AstronomyShop()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        super().__init__(app=app)

        self.kubectl = KubeCtl()
        self.injector = VirtualizationFaultInjector(namespace=self.app.namespace)
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The deployment `{self.faulty_service}` uses an overly aggressive liveness probe "
                "(`initialDelaySeconds=0`, `periodSeconds=1`, `failureThreshold=1`) with "
                "`terminationGracePeriodSeconds=0`, so brief startup or transient probe failures trigger immediate "
                "pod termination. Pods are recycled too quickly to stabilize, producing repeated restart cycles and "
                "readiness instability. Users observe intermittent service drops and elevated error rates during normal traffic."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = SustainedReadinessOracle(problem=self, sustained_period=30)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_liveness_probe_too_aggressive([self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.app.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_liveness_probe_too_aggressive([self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.app.namespace}\n")

    def confirm_fault_active(self, timeout: float = 60.0) -> bool:
        """Fault-specific: inject_fault() deletes+recreates the whole Deployment with
        an aggressive liveness probe (periodSeconds=1, failureThreshold=1,
        terminationGracePeriodSeconds=0) - the actual symptom is a *restart-cycling
        pattern*, not a single stable end state. The base class's generic default
        would eventually treat this as "stable" once K8s labels it CrashLoopBackOff,
        but that can lag behind the real signal - confirm directly via restart_count
        on the fresh pod instead of waiting for that label to appear."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pods = self.kubectl.core_v1_api.list_namespaced_pod(
                self.namespace, label_selector=f"service={self.faulty_service}"
            ).items
            for pod in pods:
                for cs in pod.status.container_statuses or []:
                    if cs.restart_count and cs.restart_count >= 1:
                        return True
            time.sleep(3)
        return False
