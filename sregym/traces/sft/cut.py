"""Generic milestone-driven cutter: one oracle-verified ATIF trajectory -> a
set of SFT training examples.

Uses each problem's registered milestone *role* sequence
(``sregym.traces.sft.detectors``) instead of any problem-specific slicing
code, so this module never changes when a new problem is added -- only
``sregym/traces/sft/problems/<new_problem>.py`` does. A milestone role that
isn't reached in a given episode (e.g. an orchestrator run whose sub-agent
calls hide the evidence-gathering steps) simply yields fewer examples for
that episode instead of raising -- see ``target_port_misconfig.py`` for a
concrete case of this gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from atif_converter import Step, Trajectory

from . import problems as _problems  # noqa: F401  (import registers all detectors)
from .detectors import MilestoneRole, StepView, assign_milestones, has_detector, milestones_for, role_of

SYSTEM_PROMPT = (
    "You are a Kubernetes infrastructure operations model.\n\n"
    "Workflow:\n"
    "1. Inspect the current infrastructure state before forming a hypothesis.\n"
    "2. Collect sufficient evidence before submitting a diagnosis.\n"
    "3. Submit a diagnosis only when it is supported by observed evidence.\n"
    "4. Execute mitigation using the available tools.\n"
    "5. Verify the resulting cluster state before declaring recovery.\n"
    "6. Submit mitigation only after recovery has been verified."
)

MAX_OBSERVATION_LINES = 25

# Environment-rejection phrasings seen in real runs (e.g. the MCP kubectl
# proxy's "Command Rejected (ValueError): ..." envelope). Heuristic, not
# exhaustive -- extend as new harnesses/tools surface new rejection wording.
_REJECTION_PATTERN = re.compile(r"Command Rejected|Unsupported operator|permission denied|Forbidden:", re.IGNORECASE)


def _cut_lines(text: str, max_lines: int = MAX_OBSERVATION_LINES) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[:max_lines])


def step_views(trajectory: Trajectory) -> list[StepView]:
    """Adapt ATIF ``Step`` objects to the lightweight view detectors match against."""
    views = []
    for step in trajectory.steps:
        tool_calls = [{"name": tc.function_name, "arguments": tc.arguments} for tc in (step.tool_calls or [])]
        observations = [r.content for r in (step.observation.results if step.observation else []) if isinstance(r.content, str)]
        views.append(StepView(step.step_id, step.source, tool_calls, observations))
    return views


def _text_observations(step: Step) -> list[str]:
    if not step.observation:
        return []
    return [r.content for r in step.observation.results if isinstance(r.content, str)]


def _tool_call_msg(step: Step) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [{"name": tc.function_name, "arguments": tc.arguments} for tc in (step.tool_calls or [])],
    }


def _observation_msg(step: Step) -> dict:
    parts = _text_observations(step)
    return {"role": "tool", "content": "\n---\n".join(_cut_lines(p) for p in parts)}


def _context_messages(objective: str, steps: list[Step], upto_step_id: int) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": objective}]
    for s in steps:
        if s.step_id > upto_step_id:
            break
        if s.source != "agent":
            continue
        if s.tool_calls:
            msgs.append(_tool_call_msg(s))
        if _text_observations(s):
            msgs.append(_observation_msg(s))
    return msgs


@dataclass(frozen=True)
class SftExample:
    sample_id: str
    task: str
    step_range: tuple[int, int]
    messages: list[dict]
    label: str | None = None

    def to_record(self, *, episode_id: str, problem_id: str, category: str, oracle: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "sample_id": self.sample_id,
            "source": "sregym",
            "episode_id": episode_id,
            "task": self.task,
            "category": category,
            "problem": problem_id,
            "oracle": oracle,
            "step_range": list(self.step_range),
            "messages": self.messages,
        }
        if self.label:
            record["label"] = self.label
        return record


@dataclass
class _RoleSteps:
    """Steps (by step_id, in order) assigned to each milestone role."""

    by_role: dict[MilestoneRole, list[Step]] = field(default_factory=dict)

    def add(self, role: MilestoneRole, step: Step) -> None:
        self.by_role.setdefault(role, []).append(step)

    def get(self, role: MilestoneRole) -> list[Step]:
        return self.by_role.get(role, [])


def _group_by_role(problem_id: str, steps: list[Step], assignment: dict[int, str]) -> _RoleSteps:
    grouped = _RoleSteps()
    for step in steps:
        milestone_id = assignment.get(step.step_id)
        if milestone_id is None:
            continue
        grouped.add(role_of(problem_id, milestone_id), step)
    return grouped


def build_examples(trajectory: Trajectory) -> list[SftExample]:
    """Cut one oracle-verified trajectory into SFT examples.

    Raises ``KeyError`` if ``trajectory``'s ``problem_id`` has no registered
    detector -- callers mining in bulk should check ``has_detector()`` first
    and skip/log rather than let this propagate.
    """
    sregym = (trajectory.extra or {}).get("sregym", {})
    problem_id = sregym.get("problem_id")
    if not problem_id or not has_detector(problem_id):
        raise KeyError(f"no milestone detector registered for problem_id={problem_id!r}")

    steps = trajectory.steps
    objective = steps[0].message if isinstance(steps[0].message, str) else ""
    views = step_views(trajectory)
    assignment = assign_milestones(problem_id, views)
    grouped = _group_by_role(problem_id, steps, assignment)
    oracle = {k: v for k, v in sregym.items() if k in ("diagnosis_success", "mitigation_success")}
    episode_id = trajectory.trajectory_id or ""

    examples: list[SftExample] = []

    # --- tool_selection: one example per consecutive pair of *investigative*
    # milestones (OBSERVE/EVIDENCE) that both actually occurred, cutting
    # context at the end of the earlier one and targeting the first tool-call
    # step of the next. Restricted to investigative roles so this doesn't
    # duplicate action_selection/verification (which already cover the
    # diagnosis->mitigation and mitigation->submit transitions). ---
    milestones = milestones_for(problem_id) or []
    reached_ids = set(assignment.values())
    _investigative = {MilestoneRole.OBSERVE, MilestoneRole.EVIDENCE}
    present = [m for m in milestones if m.id in reached_ids and m.role in _investigative]
    for prev_m, next_m in zip(present, present[1:]):
        prev_steps = [s for s in steps if assignment.get(s.step_id) == prev_m.id]
        next_steps = [s for s in steps if assignment.get(s.step_id) == next_m.id and s.tool_calls]
        if not prev_steps or not next_steps:
            continue
        target = next_steps[0]
        ctx = _context_messages(objective, steps, prev_steps[-1].step_id)
        examples.append(
            SftExample(
                f"{episode_id}_tool_sel_{prev_m.id}_to_{next_m.id}",
                "tool_selection",
                (steps[0].step_id, target.step_id),
                ctx + [_tool_call_msg(target)],
            )
        )

    # --- state_interpretation: evidence text -> diagnosis text (both real,
    # never fabricated -- skipped if either side is empty). ---
    evidence_text = "\n---\n".join(
        _cut_lines(o) for s in grouped.get(MilestoneRole.EVIDENCE) for o in _text_observations(s)
    )
    diagnosis_steps = grouped.get(MilestoneRole.DIAGNOSIS)
    diagnosis_text = ""
    if diagnosis_steps and diagnosis_steps[0].tool_calls:
        diagnosis_text = str(diagnosis_steps[0].tool_calls[0].arguments.get("ans", "") or "")
    if evidence_text.strip() and diagnosis_text.strip():
        diag_step = diagnosis_steps[0]
        examples.append(
            SftExample(
                f"{episode_id}_state_interpretation",
                "state_interpretation",
                (grouped.get(MilestoneRole.EVIDENCE)[0].step_id, diag_step.step_id),
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Objective: {objective}\n\nEvidence gathered so far:\n{evidence_text}\n\n"
                            "What is the root cause, and which component is at fault?"
                        ),
                    },
                    {"role": "assistant", "content": diagnosis_text},
                ],
            )
        )

    # --- action_selection: confirmed diagnosis -> real mitigation tool call. ---
    action_steps = grouped.get(MilestoneRole.MITIGATION_ACTION)
    if diagnosis_text.strip() and action_steps and action_steps[0].tool_calls:
        action_step = action_steps[0]
        examples.append(
            SftExample(
                f"{episode_id}_action_selection",
                "action_selection",
                (diagnosis_steps[0].step_id, action_step.step_id),
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Confirmed diagnosis: {diagnosis_text}\n\nApply the fix."},
                    _tool_call_msg(action_step),
                ],
            )
        )

    # --- verification: after the fix, the real next action the agent took
    # (a re-check), not an invented natural-language answer. Needs at least
    # two post-action-check steps; absent for episodes with no observable
    # re-check (e.g. an orchestrator run whose remediation sub-agent verifies
    # internally). ---
    check_steps = [s for s in grouped.get(MilestoneRole.POST_ACTION_CHECK) if s.tool_calls]
    if len(check_steps) >= 2 and action_steps:
        first_check, second_check = check_steps[0], check_steps[1]
        ctx = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Action just taken: {action_steps[0].tool_calls[0].arguments}"},
            _observation_msg(action_steps[0]) if _text_observations(action_steps[0]) else {"role": "tool", "content": "(applied)"},
            _tool_call_msg(first_check),
        ]
        if _text_observations(first_check):
            ctx.append(_observation_msg(first_check))
        examples.append(
            SftExample(
                f"{episode_id}_verification",
                "verification",
                (first_check.step_id, second_check.step_id),
                ctx + [_tool_call_msg(second_check)],
            )
        )

    # --- negative_invalid_action: real actions the environment itself
    # rejected, kept (labeled) rather than discarded -- useful later for
    # preference tuning / DPO. ---
    for s in steps:
        if s.source == "agent" and s.tool_calls and any(_REJECTION_PATTERN.search(o) for o in _text_observations(s)):
            examples.append(
                SftExample(
                    f"{episode_id}_negative_step{s.step_id}",
                    "negative_invalid_action",
                    (s.step_id, s.step_id),
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": "Verify the mitigation is effective before declaring success."},
                        _tool_call_msg(s),
                        _observation_msg(s),
                    ],
                    label="rejected_by_environment",
                )
            )

    # --- full_trajectory: whole episode, agent-authored text kept verbatim. ---
    full_msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": objective}]
    for s in steps[1:]:
        if s.tool_calls:
            full_msgs.append(_tool_call_msg(s))
        if _text_observations(s):
            full_msgs.append(_observation_msg(s))
        if s.message and isinstance(s.message, str) and s.source == "agent" and not s.tool_calls:
            full_msgs.append({"role": "assistant", "content": s.message})
    examples.append(
        SftExample(f"{episode_id}_full_trajectory", "full_trajectory", (steps[0].step_id, steps[-1].step_id), full_msgs)
    )

    return [e for e in examples]


def build_records(trajectory: Trajectory, *, category: str) -> list[dict[str, Any]]:
    """``build_examples`` + the dataset-metadata envelope, ready for ``train.jsonl``."""
    sregym = (trajectory.extra or {}).get("sregym", {})
    problem_id = sregym.get("problem_id", "")
    oracle = {k: v for k, v in sregym.items() if k in ("diagnosis_success", "mitigation_success")}
    episode_id = trajectory.trajectory_id or ""
    return [
        ex.to_record(episode_id=episode_id, problem_id=problem_id, category=category, oracle=oracle)
        for ex in build_examples(trajectory)
    ]
