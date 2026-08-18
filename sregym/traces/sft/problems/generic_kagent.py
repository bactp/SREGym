"""Generic milestone detector for the ``kagent`` agent family, registered as
the default for every problem_id that doesn't have a hand-tuned detector of
its own (see ``target_port_misconfig.py`` for the more precise, per-problem
alternative once real runs exist for a given problem).

Why generic instead of one hand-tuned module per problem: as of this writing
only 5 of SREGym's 117 registered problem_ids have any real run recorded in
``traces.db`` (see ``sregym/conductor/problems/registry.py``'s
``PROBLEM_REGISTRY`` / ``docs/problem-catalog.md``). Writing a per-problem
regex set for the other 112 would mean guessing at commands/log text nobody
has actually seen -- exactly the "fabricated content" this pipeline is meant
to avoid. Instead, this detector uses the structure every kagent-family
problem shares (the SREGym harness's own tool surface), verified against the
5 problems that do have real runs (target_port, pod_anti_affinity_deadlock,
mutating_webhook_resource_limits, liveness_probe_too_aggressive,
service_dns_resolution_failure):

* ``submit``/``submit_tool`` with a non-empty ``ans`` -> diagnosis; empty
  ``ans`` -> mitigation submitted (same convention observed in every run).
* A "get_*" observability tool (``get_alerts``, ``get_services``,
  ``get_metrics``, ``get_operations``, ``get_traces``, ...), a tool whose
  name says it's read-only (``exec_read_only_kubectl_cmd``), or a kubectl
  command using a read-only verb (get/describe/logs/top/explain/events) ->
  investigative (observe the first such step, evidence afterwards).
* A kubectl command using a mutating verb (patch/apply/replace/delete/edit/
  scale/label/annotate/cordon/drain/taint/create/expose, or `rollout
  restart`/`rollout undo`) -> mitigation action. Deliberately excludes
  `kubectl exec` and `kubectl run` -- both were observed used purely for
  read-only connectivity probing (`curl`, `nslookup`, `getent hosts`) in real
  runs, not as the actual fix.
* A ``kagent__NS__*rca*`` sub-agent delegation -> investigative (opaque);
  a ``kagent__NS__*remediation*`` delegation -> mitigation action (opaque).
* Anything investigative-shaped arriving after mitigation_action has already
  fired -> post-action check, not a rewind (same "reached" trick as the
  target_port detector).

Once a given problem_id accumulates enough real runs to reveal problem-
specific evidence patterns (the way target_port's Service/targetPort
mismatch did), replace its entry here with a dedicated, hand-tuned module.
"""

from __future__ import annotations

import re

from ..detectors import Milestone, MilestoneRole, StepView, has_detector, register

MILESTONES = [
    Milestone("observe", MilestoneRole.OBSERVE, "Initial investigation"),
    Milestone("evidence", MilestoneRole.EVIDENCE, "Further evidence-gathering"),
    Milestone("diagnosis", MilestoneRole.DIAGNOSIS, "Diagnosis submitted"),
    Milestone("mitigation_action", MilestoneRole.MITIGATION_ACTION, "Mitigation action executed"),
    Milestone("post_action_check", MilestoneRole.POST_ACTION_CHECK, "Post-action state re-checked"),
    Milestone("mitigation_submitted", MilestoneRole.MITIGATION_SUBMITTED, "Mitigation submitted"),
]

_WRITE_VERB = re.compile(
    r"kubectl\b.*?\b(patch|apply|replace|delete|edit|scale|label|annotate|cordon|drain|taint|expose|create)\b",
    re.IGNORECASE | re.DOTALL,
)
_ROLLOUT_MUTATING = re.compile(r"kubectl\b.*?\brollout\s+(restart|undo)\b", re.IGNORECASE | re.DOTALL)
_RCA_DELEGATE = re.compile(r"rca", re.IGNORECASE)
_REMEDIATION_DELEGATE = re.compile(r"remediation", re.IGNORECASE)
_READ_ONLY_TOOL_NAME = re.compile(r"read.?only", re.IGNORECASE)


def _is_submit(step: StepView) -> bool:
    return any("submit" in name.lower() for name in step.tool_names)


def _is_write_command(step: StepView) -> bool:
    for tc in step.tool_calls:
        args = tc.get("arguments") or {}
        for key in ("cmd", "command"):
            val = args.get(key)
            if val and (_WRITE_VERB.search(str(val)) or _ROLLOUT_MUTATING.search(str(val))):
                return True
    return False


def _is_investigative(step: StepView) -> bool:
    if not step.tool_calls:
        return False
    for name in step.tool_names:
        lname = name.lower()
        if lname.startswith("get_") or _READ_ONLY_TOOL_NAME.search(lname):
            return True
    if step.any_command_matches(r"kubectl\b.*?\b(get|describe|logs|top|explain|wait)\b"):
        return True
    return step.any_command_matches(r"kubectl\b.*?\brollout\s+(status|history)\b")


def _detect(step: StepView, reached: frozenset[str]) -> str | None:
    if _is_submit(step):
        ans = step.first_tool_call_arg("ans")
        return "diagnosis" if ans else "mitigation_submitted"

    if _is_write_command(step) or any(_REMEDIATION_DELEGATE.search(n) for n in step.tool_names):
        return "mitigation_action"

    if any(_RCA_DELEGATE.search(n) for n in step.tool_names):
        return "evidence"  # opaque delegated investigation

    if "mitigation_action" in reached:
        # Same read-shaped commands as observe/evidence, but the fix has
        # already been applied -- this is a re-check, not new investigation.
        # A non-investigative step with no matching signal just inherits
        # whatever milestone is already current (returning None here, not
        # forcing post_action_check on e.g. a plain wrap-up message).
        return "post_action_check" if _is_investigative(step) else None

    if _is_investigative(step):
        return "evidence" if "observe" in reached else "observe"

    return None


# Every registered SREGym problem_id as of docs/problem-catalog.md /
# sregym/conductor/problems/registry.py's PROBLEM_REGISTRY (2026-08-18). Not
# imported live from the registry: constructing it requires a real
# kubeconfig (KubeCtl() fails fast without one), which this offline data
# pipeline must not depend on. Re-derive this list (grep registry.py for
# `^\s*"<id>":`) if new problems are added.
ALL_PROBLEM_IDS: list[str] = [
    "admission_webhook_outage_hotel_reservation",
    "admission_webhook_tls_mismatch_hotel_reservation",
    "assign_to_non_existent_node",
    "astronomy_shop_ad_service_failure",
    "astronomy_shop_ad_service_high_cpu",
    "astronomy_shop_ad_service_image_slow_load",
    "astronomy_shop_ad_service_manual_gc",
    "astronomy_shop_cart_service_failure",
    "astronomy_shop_failed_readiness_probe",
    "astronomy_shop_payment_service_failure",
    "astronomy_shop_payment_service_unreachable",
    "astronomy_shop_product_catalog_service_failure",
    "auth_miss_mongodb",
    "calico_route_reflector_label_drift_hotel_reservation",
    "capacity_decrease_rpc_retry_storm",
    "configmap_drift_hotel_reservation",
    "cronjob_sidecar_blocks_completion_hotel_reservation",
    "cumulative_admission_webhook_timeout_hotel_reservation",
    "dev_shm_exhaustion_hotel_reservation",
    "duplicate_pvc_mounts_astronomy_shop",
    "duplicate_pvc_mounts_hotel_reservation",
    "duplicate_pvc_mounts_social_network",
    "edge_request_filter_cpu_saturation",
    "env_variable_shadowing_astronomy_shop",
    "ephemeral_port_range_hotel_reservation",
    "expired_tls_hotel_reservation",
    "faulty_image_correlated",
    "file_descriptor_exhaustion",
    "finalizer_deadlock_controller_hotel_reservation",
    "gc_capacity_degradation",
    "hpa_missing_effective_cpu_request_hotel_reservation",
    "incorrect_image",
    "incorrect_port_assignment",
    "ingress_misroute",
    "init_container_dependency_hang_astronomy_shop",
    "init_container_dependency_hang_hotel_reservation",
    "init_container_dependency_hang_social_network",
    "internal_traffic_policy_local_astronomy_shop",
    "kafka_poison_pill_hol_block",
    "kafka_queue_problems",
    "kubelet_crash",
    "kubelet_eviction_threshold_misconfig",
    "latent_sector_error",
    "liveness_probe_misconfiguration_astronomy_shop",
    "liveness_probe_misconfiguration_hotel_reservation",
    "liveness_probe_misconfiguration_social_network",
    "liveness_probe_too_aggressive_astronomy_shop",
    "liveness_probe_too_aggressive_hotel_reservation",
    "liveness_probe_too_aggressive_social_network",
    "loadgenerator_flood_homepage",
    "load_spike_rpc_retry_storm",
    "misconfig_app_hotel_res",
    "missing_configmap_hotel_reservation",
    "missing_configmap_social_network",
    "missing_env_variable_astronomy_shop",
    "missing_service_astronomy_shop",
    "missing_service_hotel_reservation",
    "missing_service_social_network",
    "mutating_webhook_resource_limits_social_network",
    "namespace_memory_limit",
    "network_policy_block",
    "node_clock_drift_hotel_reservation",
    "node_conntrack_exhaustion_hotel_reservation",
    "operator_invalid_affinity_toleration",
    "operator_non_existent_storage",
    "operator_overload_replicas",
    "operator_security_context_fault",
    "operator_wrong_operator_image",
    "operator_wrong_update_strategy_fault",
    "persistent_volume_affinity_violation",
    "pod_anti_affinity_deadlock",
    "pod_cidr_exhaustion_hotel_reservation",
    "priority_preemption_cascade_hotel_reservation",
    "psa_restricted_blocks_recreation_hotel_reservation",
    "pvc_claim_mismatch",
    "rbac_misconfiguration",
    "readiness_probe_misconfiguration_astronomy_shop",
    "readiness_probe_misconfiguration_hotel_reservation",
    "readiness_probe_misconfiguration_social_network",
    "resource_request_too_large",
    "resource_request_too_small",
    "revoke_auth_mongodb-1",
    "revoke_auth_mongodb-2",
    "rolling_update_misconfigured_hotel_reservation",
    "rolling_update_misconfigured_social_network",
    "scale_pod_zero_social_net",
    "secret_rotation_stale_env_credentials_astronomy_shop",
    "service_dns_resolution_failure_astronomy_shop",
    "service_dns_resolution_failure_social_network",
    "service_port_conflict_astronomy_shop",
    "service_port_conflict_hotel_reservation",
    "service_port_conflict_social_network",
    "service_wrong_pod_selection_hotel_reservation",
    "sidecar_port_conflict_astronomy_shop",
    "sidecar_port_conflict_hotel_reservation",
    "sidecar_port_conflict_social_network",
    "silent_data_corruption",
    "stale_coredns_config_astronomy_shop",
    "stale_coredns_config_social_network",
    "storage_user_unregistered-1",
    "storage_user_unregistered-2",
    "taint_no_toleration_social_network",
    "trainticket_f17_nested_sql_select_clause_error",
    "trainticket_f22_sql_column_name_mismatch_error",
    "unschedulable_incorrect_port_assignment",
    "update_incompatible_correlated",
    "valkey_auth_disruption",
    "valkey_memory_disruption",
    "workload_imbalance",
    "wrong_bin_usage",
    "wrong_dns_policy_astronomy_shop",
    "wrong_dns_policy_hotel_reservation",
    "wrong_dns_policy_social_network",
    "wrong_service_selector_astronomy_shop",
    "wrong_service_selector_hotel_reservation",
    "wrong_service_selector_social_network",
    # NOTE: "k8s_target_port-misconfig" deliberately excluded -- it keeps the
    # more precise, hand-tuned detector in target_port_misconfig.py.
]

for _problem_id in ALL_PROBLEM_IDS:
    if not has_detector(_problem_id):
        register(_problem_id, MILESTONES, _detect)
