"""Integration test: run the generic cutter against the two real,
oracle-verified trajectories the detector was authored from, pulled straight
out of ``results/traces.db``. Skips if that (gitignored, local-only) database
isn't present -- CI/clean-checkout coverage lives in ``test_detectors.py``.
"""

from pathlib import Path

import pytest

from sregym.traces import store
from sregym.traces.sft import cut

DB_PATH = Path(__file__).resolve().parents[3] / "results" / "traces.db"

FLAT_TRAJECTORY_ID = "0803_0941/kagent/k8s_target_port-misconfig/run_1"
ORCHESTRATOR_TRAJECTORY_ID = "0803_0928/kagent-orchestrator/k8s_target_port-misconfig/run_1"

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="results/traces.db not present (gitignored, local-only)")


def _load(trajectory_id: str):
    traj = store.get(trajectory_id, DB_PATH)
    if traj is None:
        pytest.skip(f"{trajectory_id} not ingested in {DB_PATH}")
    return traj


def test_flat_run_produces_all_task_types():
    traj = _load(FLAT_TRAJECTORY_ID)
    records = cut.build_records(traj, category="k8s_service_networking")
    tasks = {r["task"] for r in records}
    assert tasks >= {
        "tool_selection",
        "state_interpretation",
        "action_selection",
        "verification",
        "negative_invalid_action",
        "full_trajectory",
    }
    # Metadata lives outside `messages`, never inside the trained content.
    for r in records:
        assert "messages" in r and isinstance(r["messages"], list)
        assert r["oracle"]["mitigation_success"] is True
        assert all("task" != m.get("role") for m in r["messages"])


def test_orchestrator_run_only_yields_coverage_it_actually_has():
    """The orchestrator shape hides its sub-agents' tool calls, so it must
    NOT fabricate tool_selection/verification examples that would require
    evidence never captured in this trajectory's ATIF steps."""
    traj = _load(ORCHESTRATOR_TRAJECTORY_ID)
    records = cut.build_records(traj, category="k8s_service_networking")
    tasks = {r["task"] for r in records}
    assert "tool_selection" not in tasks
    assert "verification" not in tasks
    assert "action_selection" in tasks
    assert "full_trajectory" in tasks


def test_action_selection_uses_real_diagnosis_and_real_tool_call():
    traj = _load(FLAT_TRAJECTORY_ID)
    records = cut.build_records(traj, category="k8s_service_networking")
    action = next(r for r in records if r["task"] == "action_selection")
    user_msg = next(m for m in action["messages"] if m["role"] == "user")
    assistant_msg = next(m for m in action["messages"] if m["role"] == "assistant")
    assert "targetPort" in user_msg["content"]
    assert assistant_msg["tool_calls"][0]["name"] == "exec_kubectl_cmd_safely"
    assert "patch svc user-service" in assistant_msg["tool_calls"][0]["arguments"]["cmd"]
