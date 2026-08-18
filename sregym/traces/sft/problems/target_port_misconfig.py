"""Milestone detector for ``k8s_target_port-misconfig``.

Authored by reading two real, oracle-verified episodes of this problem:

* ``0803_0941/kagent/k8s_target_port-misconfig/run_1`` -- a flat agent
  (``kagent``) that calls raw ``exec_kubectl_cmd_safely``/``get_alerts``
  steps directly, 24 steps end to end.
* ``0803_0928/kagent-orchestrator/k8s_target_port-misconfig/run_1`` -- an
  orchestrator agent that delegates investigation/remediation to opaque
  sub-agent calls (``kagent__NS__rca_agent`` / ``kagent__NS__remediation_agent``),
  6 steps end to end, with no observable evidence-gathering commands at all.

The same detector below must handle both shapes, which is exactly the
generalization a content-based detector buys over a hand-written
``step_id -> milestone`` table: the two episodes don't share a single step
count or ordering, only the same problem semantics.

Known coverage gap: the orchestrator shape never contributes tool_selection /
state_interpretation / verification examples, because the sub-agent's own
tool calls and observations are not captured anywhere in its ATIF trajectory
(no content on the delegating step's observation, no
``subagent_trajectory_ref``). It can still contribute action_selection
(the diagnosis text) and full_trajectory examples.
"""

from __future__ import annotations

from ..detectors import Milestone, MilestoneRole, StepView, register

MILESTONES = [
    Milestone("M1_initial_state", MilestoneRole.OBSERVE, "Pod/workload runtime state inspected"),
    Milestone(
        "M2_symptom_evidence",
        MilestoneRole.EVIDENCE,
        "Error symptom observed (connection-refused evidence in alerts/logs)",
    ),
    Milestone(
        "M3_root_cause_evidence",
        MilestoneRole.EVIDENCE,
        "Service targetPort vs. container port mismatch inspected",
    ),
    Milestone("M4_diagnosis", MilestoneRole.DIAGNOSIS, "Diagnosis submitted"),
    Milestone("M5_mitigation_action", MilestoneRole.MITIGATION_ACTION, "Mitigation action executed (Service patched)"),
    Milestone("M6_post_action_check", MilestoneRole.POST_ACTION_CHECK, "Post-action state re-checked"),
    Milestone("M7_mitigation_submitted", MilestoneRole.MITIGATION_SUBMITTED, "Mitigation submitted"),
]

_SVC_OR_ENDPOINTS = r"kubectl\s+get\s+(svc|service|endpoints)\b"
_CONNECTION_REFUSED = r"[Cc]onnection refused|refused to"


def _detect(step: StepView, reached: frozenset[str]) -> str | None:
    if step.any_tool_name_is("submit"):
        ans = step.first_tool_call_arg("ans")
        return "M4_diagnosis" if ans else "M7_mitigation_submitted"

    if step.any_tool_name_is("kagent__NS__remediation_agent") or step.any_command_matches(r"kubectl\s+patch\s+svc"):
        return "M5_mitigation_action"

    if step.any_tool_name_is("kagent__NS__rca_agent"):
        # Opaque delegated investigation: collapse M1-M3 into the one signal
        # we can actually observe from the orchestrator's own trajectory.
        return "M3_root_cause_evidence"

    if "M5_mitigation_action" in reached:
        # Same evidence-gathering commands as M1-M3, but the fix has already
        # been applied, so any further investigation is a re-check.
        return "M6_post_action_check"

    if step.any_command_matches(_SVC_OR_ENDPOINTS) or step.any_observation_matches(r"targetPort:\s*\d+"):
        return "M3_root_cause_evidence"

    if step.any_tool_name_is("get_alerts") or step.any_observation_matches(_CONNECTION_REFUSED):
        return "M2_symptom_evidence"

    if step.any_command_matches(r"kubectl\s+get\s+pods\b"):
        return "M1_initial_state"

    return None


register("k8s_target_port-misconfig", MILESTONES, _detect)
