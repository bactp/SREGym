"""Unit tests for the generic kagent-family detector (the fallback used for
every problem_id without a hand-tuned detector). Command/tool-name snippets
below are copied verbatim from real runs across 4 different real problems
(mutating_webhook_resource_limits, liveness_probe_too_aggressive,
pod_anti_affinity_deadlock, service_dns_resolution_failure) -- see
generic_kagent.py's module docstring for exactly which.
"""

from sregym.traces.sft import problems  # noqa: F401  (registers detectors)
from sregym.traces.sft.detectors import assign_milestones
from sregym.traces.sft.problems.generic_kagent import ALL_PROBLEM_IDS, _is_investigative, _is_write_command
from tests.traces.sft.test_detectors import _agent

PROBLEM_ID = "mutating_webhook_resource_limits_social_network"  # any generic-covered id


def test_every_registered_id_is_actually_in_the_registry_list():
    # Regression guard for the hand-maintained ALL_PROBLEM_IDS list: it must
    # not silently drift from what's actually registered.
    assert len(ALL_PROBLEM_IDS) == 116  # 117 total - target_port's own detector


def test_read_verbs_are_investigative_not_mitigating():
    for cmd in [
        "kubectl get pods -n social-network -o wide",
        "kubectl describe pod nginx-thrift-7c86df44b-jg5vn -n social-network",
        "kubectl logs nginx-thrift-7c86df44b-jg5vn -n social-network -c alpine-container",
        "kubectl -n kube-system get configmap coredns -o yaml",
        "kubectl get events -n social-network --sort-by='.lastTimestamp'",
    ]:
        step = _agent(2, "exec_kubectl_cmd_safely", {"cmd": cmd})
        assert _is_investigative(step) is True, cmd
        assert _is_write_command(step) is False, cmd


def test_write_verbs_are_mitigating():
    for cmd in [
        "kubectl patch deployment nginx-thrift -n social-network --type='strategic' -p '{...}'",
        "kubectl rollout restart deployment/nginx-thrift -n social-network",
        "kubectl -n kube-system apply -f - <<'EOF'\napiVersion: v1\n...",
        "kubectl -n kube-system patch configmap coredns --type=json -p='[...]'",
    ]:
        step = _agent(2, "exec_kubectl_cmd_safely", {"cmd": cmd})
        assert _is_write_command(step) is True, cmd


def test_rollout_status_is_a_check_not_a_mitigating_action():
    """`rollout restart` mutates; `rollout status` only polls progress -- a
    real run used both back to back and only the first should count."""
    step = _agent(2, "exec_kubectl_cmd_safely", {"cmd": "kubectl -n kube-system rollout status deployment coredns --timeout=120s"})
    assert _is_write_command(step) is False
    assert _is_investigative(step) is True


def test_run_and_exec_are_not_treated_as_mitigation():
    """Real runs used `kubectl run`/`kubectl exec` purely for read-only
    connectivity probing (curl, nslookup, getent) -- must not be mistaken
    for the actual fix."""
    for cmd in [
        "kubectl -n social-network run dns-test --image=infoblox/dnstools --restart=Never --command -- sleep 3600",
        "kubectl -n social-network exec dns-test -- nslookup user-service.social-network.svc.cluster.local",
        "kubectl exec -n social-network user-service-x -- curl -sS localhost:9090/health",
    ]:
        step = _agent(2, "exec_kubectl_cmd_safely", {"cmd": cmd})
        assert _is_write_command(step) is False, cmd


def test_observability_tools_are_investigative():
    for name in ["get_alerts", "get_services", "get_metrics", "get_operations", "get_traces"]:
        step = _agent(2, name, {})
        assert _is_investigative(step) is True, name


def test_read_only_named_tool_is_investigative_regardless_of_command():
    step = _agent(2, "exec_read_only_kubectl_cmd", {"cmd": "kubectl get pods -n social-network -o wide"})
    assert _is_investigative(step) is True


def test_full_episode_with_delegated_subagents_and_submit_tool_variant():
    """Mirrors 0804_0730/kagent-orchestrator/pod_anti_affinity_deadlock/run_1:
    3 steps, rca_agent -> submit(ans=text) -> remediation_agent, plus a
    `submit_tool` name variant (seen in the DNS-resolution run) instead of
    plain `submit`."""
    steps = [
        _agent(2, "kagent__NS__rca_agent", {"request": "Diagnose..."}),
        _agent(3, "submit_tool", {"ans": "user-service has a strict podAntiAffinity..."}, obs='{"status":"200"}'),
        _agent(4, "kagent__NS__remediation_agent", {"request": "Root cause: ..."}),
        _agent(5, "submit", {"ans": ""}, obs='{"status":"200"}'),
    ]
    assignment = assign_milestones(PROBLEM_ID, steps)
    assert assignment[2] == "evidence"
    assert assignment[3] == "diagnosis"
    assert assignment[4] == "mitigation_action"
    assert assignment[5] == "mitigation_submitted"


def test_post_action_recheck_reclassifies_read_commands():
    steps = [
        _agent(2, "exec_kubectl_cmd_safely", {"cmd": "kubectl get pods -n social-network"}),
        _agent(3, "submit", {"ans": "diagnosis text"}),
        _agent(4, "exec_kubectl_cmd_safely", {"cmd": "kubectl patch deployment aux-service --type=merge -p '{}'"}),
        _agent(5, "exec_kubectl_cmd_safely", {"cmd": "kubectl rollout restart deployment aux-service"}),
        _agent(6, "exec_kubectl_cmd_safely", {"cmd": "kubectl get pods -n social-network -o wide"}),
        _agent(7, "get_alerts", {}),
        _agent(8, "submit", {"ans": ""}),
    ]
    assignment = assign_milestones(PROBLEM_ID, steps)
    assert assignment[2] == "observe"
    assert assignment[3] == "diagnosis"
    assert assignment[4] == "mitigation_action"
    assert assignment[5] == "mitigation_action"  # a second write command right after the first
    assert assignment[6] == "post_action_check"
    assert assignment[7] == "post_action_check"
    assert assignment[8] == "mitigation_submitted"
