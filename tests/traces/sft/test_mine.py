"""Unit tests for the batch miner, against a synthetic in-memory-style DB
(not the real gitignored results/traces.db) so this suite runs on a clean
checkout."""

from atif_converter import Trajectory

from sregym.traces import store
from sregym.traces.sft import mine


def _traj(trajectory_id: str, *, problem_id: str, mitigation_success: bool | None) -> Trajectory:
    sregym: dict = {"problem_id": problem_id, "run": 1}
    if mitigation_success is not None:
        sregym["mitigation_success"] = mitigation_success
        sregym["diagnosis_success"] = mitigation_success
    return Trajectory.model_validate(
        {
            "schema_version": "ATIF-v1.7",
            "trajectory_id": trajectory_id,
            "agent": {"name": "kagent", "version": "1.0"},
            "steps": [
                {"step_id": 1, "source": "user", "message": "diagnose the issue"},
                {
                    "step_id": 2,
                    "source": "agent",
                    "message": "checking",
                    "tool_calls": [
                        {"tool_call_id": "c1", "function_name": "exec_kubectl_cmd_safely", "arguments": {"cmd": "kubectl get pods"}}
                    ],
                    "observation": {"results": [{"source_call_id": "c1", "content": "pod/foo Running"}]},
                },
            ],
            "extra": {"sregym": sregym},
        }
    )


def test_mine_only_includes_mitigation_verified_episodes_with_a_detector(tmp_path):
    db = tmp_path / "traces.db"
    store.upsert(_traj("a", problem_id="k8s_target_port-misconfig", mitigation_success=True), db)
    store.upsert(_traj("b", problem_id="k8s_target_port-misconfig", mitigation_success=False), db)
    store.upsert(_traj("c", problem_id="k8s_target_port-misconfig", mitigation_success=None), db)
    store.upsert(_traj("d", problem_id="some_problem_without_a_detector", mitigation_success=True), db)

    records, tally = mine.mine(db)

    assert {r["episode_id"] for r in records} == {"a"}
    assert tally["mined"] == 1
    assert tally["no_detector"] == 1  # "d"
    # "b" (mitigation_success=False) and "c" (unknown) are excluded by the
    # store query itself (mitigation_success=True), not by the miner.


def test_mine_respects_problem_filter(tmp_path):
    db = tmp_path / "traces.db"
    store.upsert(_traj("a", problem_id="k8s_target_port-misconfig", mitigation_success=True), db)

    records, tally = mine.mine(db, problem_id="some_other_problem")
    assert records == []
    assert tally["mined"] == 0
