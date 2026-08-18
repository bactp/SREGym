"""Unit tests for the milestone-detector framework.

Step content below is copied verbatim (tool name / cmd / observation
snippets) from two real, oracle-verified episodes of
``k8s_target_port-misconfig`` -- a flat agent (24 steps, raw kubectl calls)
and an orchestrator agent (6 steps, opaque sub-agent delegation) -- so this
test proves the *same* registered detector segments both shapes correctly,
which is the whole point of content-based (not step-index-based) milestones.
"""

from sregym.traces.sft import problems  # noqa: F401  (registers detectors)
from sregym.traces.sft.detectors import StepView, assign_milestones


def _agent(step_id, name=None, args=None, obs=None):
    tool_calls = [{"name": name, "arguments": args or {}}] if name else []
    return StepView(step_id, "agent", tool_calls, [obs] if obs else [])


def _flat_episode():
    """Mirrors 0803_0941/kagent/k8s_target_port-misconfig/run_1 (24 steps)."""
    return [
        _agent(2, "exec_kubectl_cmd_safely", {"cmd": "kubectl get pods -n social-network -o wide"}, "NAME READY..."),
        _agent(3, "get_alerts", {}, "[{'labels': {'alertname': 'DeploymentNotReady', ...}}]"),
        _agent(4, "exec_kubectl_cmd_safely", {"cmd": "kubectl get deployments -n social-network -o wide"}, "NAME READY..."),
        _agent(5, "get_services", {}, None),
        _agent(6, "exec_kubectl_cmd_safely", {"cmd": "kubectl logs nginx-thrift-x"}, "connect() failed: Connection refused"),
        _agent(7, "exec_kubectl_cmd_safely", {"cmd": "kubectl describe pod nginx-thrift-x"}, "Name: nginx-thrift-x ..."),
        _agent(8, "exec_kubectl_cmd_safely", {"cmd": "kubectl logs compose-post-service-x"}, "Thrift connection refused"),
        _agent(9, "exec_kubectl_cmd_safely", {"cmd": "kubectl get svc -n social-network -o wide"}, "NAME TYPE ..."),
        _agent(10, "exec_kubectl_cmd_safely", {"cmd": "kubectl logs user-service-x"}, "mongodb connection error"),
        _agent(11, "exec_kubectl_cmd_safely", {"cmd": "kubectl get endpoints -n social-network user-service -o yaml"}, "port: 9999"),
        _agent(12, "exec_kubectl_cmd_safely", {"cmd": "kubectl get svc user-service -n social-network -o yaml"}, "targetPort: 9090"),
        _agent(13, "submit", {"ans": "Diagnosis: Service targetPort mismatch..."}, '{"status":"200"}'),
        _agent(14, "exec_kubectl_cmd_safely", {"cmd": "kubectl patch svc user-service --type=json -p=[...]"}, "service/user-service patched"),
        _agent(15, "exec_kubectl_cmd_safely", {"cmd": "kubectl get endpoints -n social-network user-service -o yaml"}, "port: 9999"),
        _agent(16, "exec_kubectl_cmd_safely", {"cmd": "kubectl logs compose-post-service-x --tail=200"}, "Connection refused"),
        _agent(21, "exec_kubectl_cmd_safely", {"cmd": "kubectl exec user-service-x -- curl localhost:9090/health"}, "Command Rejected (ValueError): Unsupported operator kind: list"),
        _agent(22, "exec_kubectl_cmd_safely", {"cmd": "kubectl exec user-service-x -- curl localhost:9090/health"}, "Command Rejected (ValueError): Unsupported operator kind: list"),
        _agent(23, "submit", {"ans": ""}, '{"status":"200"}'),
    ]


def _orchestrator_episode():
    """Mirrors 0803_0928/kagent-orchestrator/k8s_target_port-misconfig/run_1 (6 steps)."""
    return [
        _agent(2, "kagent__NS__rca_agent", {"request": "Diagnose incident..."}, None),
        _agent(3, "submit", {"ans": "Multiple microservices cannot reach user-service..."}, '{"status":"200"}'),
        _agent(4, "kagent__NS__remediation_agent", {"request": "Root cause: ..."}, None),
        _agent(5, "submit", {"ans": ""}, '{"status":"200"}'),
        StepView(6, "agent", [], []),  # wrap-up message, no tool call
    ]


def test_flat_episode_matches_hand_authored_mapping():
    assignment = assign_milestones("k8s_target_port-misconfig", _flat_episode())
    assert assignment[2] == "M1_initial_state"
    assert assignment[9] == "M3_root_cause_evidence"
    assert assignment[11] == "M3_root_cause_evidence"
    assert assignment[12] == "M3_root_cause_evidence"
    assert assignment[13] == "M4_diagnosis"
    assert assignment[14] == "M5_mitigation_action"
    # Re-checks after the patch (identical command shape to M3) must resolve
    # to the post-action-check role, not stay tagged as root-cause evidence.
    assert assignment[15] == "M6_post_action_check"
    assert assignment[16] == "M6_post_action_check"
    assert assignment[21] == "M6_post_action_check"  # the rejected curl attempt
    assert assignment[23] == "M7_mitigation_submitted"


def test_orchestrator_episode_collapses_hidden_investigation():
    assignment = assign_milestones("k8s_target_port-misconfig", _orchestrator_episode())
    assert assignment[2] == "M3_root_cause_evidence"  # opaque delegated investigation
    assert assignment[3] == "M4_diagnosis"
    assert assignment[4] == "M5_mitigation_action"
    assert assignment[5] == "M7_mitigation_submitted"
    # No M6 was ever observable in this shape -- must not be fabricated.
    assert "M6_post_action_check" not in assignment.values()
    # The trailing wrap-up message (no tool call) inherits forward, not
    # backward, once mitigation has already been submitted.
    assert assignment[6] == "M7_mitigation_submitted"


def test_milestones_fire_in_non_decreasing_order_even_with_out_of_order_signal():
    # A step that would otherwise look like "M3 evidence" (get svc) arriving
    # after M5 has already fired must not rewind the episode back to M3.
    steps = [
        _agent(2, "exec_kubectl_cmd_safely", {"cmd": "kubectl get pods"}, "Running"),
        _agent(3, "exec_kubectl_cmd_safely", {"cmd": "kubectl patch svc user-service"}, "patched"),
        _agent(4, "exec_kubectl_cmd_safely", {"cmd": "kubectl get svc user-service -o yaml"}, "targetPort: 9090"),
    ]
    assignment = assign_milestones("k8s_target_port-misconfig", steps)
    assert assignment[4] == "M6_post_action_check"
