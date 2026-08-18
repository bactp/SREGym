"""Per-problem milestone detectors.

A detector maps one ATIF step to the operational milestone it satisfies by
matching the step's actual tool-call command / observation content -- never
its ordinal position in the trajectory. This is what makes segmentation
reusable across every run of a problem: two runs of the same problem_id can
take a different number of steps in a different order (a different agent, a
different orchestration style, a different LLM decision at the same fork)
and still segment consistently, because the detector looks at *what the step
says*, not *which number it is*.

Contrast with a hand-written ``step_id -> milestone`` lookup table (the
approach used to build one demo episode by hand): that table is valid for
exactly the one trajectory it was read off of and has to be re-authored by
hand for every other run, which does not scale past a handful of episodes.

Detectors are given the set of milestone ids already reached so far (not the
raw step index) when a command pattern is ambiguous on its own -- e.g. "get
endpoints" means root-cause investigation before a fix has been applied, and
means post-action verification after one has. That is still content/order
based, not a step-number lookup: it generalizes across runs of any length.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum


class MilestoneRole(str, Enum):
    """Canonical role a problem-specific milestone plays in the operational
    loop. The generic cutter (``sregym.traces.sft.cut``) keys off *role*, not
    off a problem's own milestone ``id`` strings, so it works unmodified for
    every registered problem."""

    OBSERVE = "observe"
    EVIDENCE = "evidence"
    DIAGNOSIS = "diagnosis"
    MITIGATION_ACTION = "mitigation_action"
    POST_ACTION_CHECK = "post_action_check"
    MITIGATION_SUBMITTED = "mitigation_submitted"


@dataclass(frozen=True)
class Milestone:
    """One evidence/action checkpoint a problem's episodes are expected to pass through."""

    id: str
    role: MilestoneRole
    description: str


@dataclass(frozen=True)
class StepView:
    """Ergonomic, detector-facing view of one ATIF step.

    Deliberately not the ATIF ``Step`` pydantic model itself: detectors only
    ever need "what command ran" / "what came back", and a plain view keeps
    problem modules free of ATIF/store import churn.
    """

    step_id: int
    source: str
    tool_calls: list[dict]  # [{"name": str, "arguments": dict}, ...]
    observations: list[str]

    @property
    def tool_names(self) -> list[str]:
        return [tc.get("name", "") for tc in self.tool_calls]

    def _commands(self, arg_keys: Iterable[str] = ("cmd", "command")) -> list[str]:
        cmds = []
        for tc in self.tool_calls:
            args = tc.get("arguments") or {}
            for key in arg_keys:
                if key in args and args[key] is not None:
                    cmds.append(str(args[key]))
        return cmds

    def any_tool_name_is(self, *names: str) -> bool:
        return any(name in self.tool_names for name in names)

    def any_command_matches(self, pattern: str) -> bool:
        return any(re.search(pattern, cmd) for cmd in self._commands())

    def any_observation_matches(self, pattern: str) -> bool:
        return any(re.search(pattern, obs) for obs in self.observations)

    def first_tool_call_arg(self, key: str) -> object | None:
        if not self.tool_calls:
            return None
        return (self.tool_calls[0].get("arguments") or {}).get(key)


# reached: the set of milestone ids already assigned earlier in this episode.
# Lets a detector disambiguate an otherwise-identical command by what has
# already happened (e.g. "get endpoints" before vs. after the fix), without
# reintroducing a step-number dependency.
Detector = Callable[[StepView, frozenset[str]], "str | None"]

_MILESTONES: dict[str, list[Milestone]] = {}
_DETECTORS: dict[str, Detector] = {}


def register(problem_id: str, milestones: list[Milestone], detector: Detector) -> None:
    """Register a problem's milestone list + detector. Call once at import time."""
    if problem_id in _DETECTORS:
        raise ValueError(f"problem_id {problem_id!r} already has a registered detector")
    _MILESTONES[problem_id] = milestones
    _DETECTORS[problem_id] = detector


def milestones_for(problem_id: str) -> list[Milestone] | None:
    return _MILESTONES.get(problem_id)


def has_detector(problem_id: str) -> bool:
    return problem_id in _DETECTORS


def registered_problem_ids() -> list[str]:
    return sorted(_DETECTORS)


def role_of(problem_id: str, milestone_id: str) -> MilestoneRole:
    for m in _MILESTONES[problem_id]:
        if m.id == milestone_id:
            return m.role
    raise KeyError(f"detector for {problem_id!r} returned unknown milestone id {milestone_id!r}")


def assign_milestones(problem_id: str, steps: list[StepView]) -> dict[int, str]:
    """Map ``step_id -> milestone id`` for every agent step in ``steps``.

    A step that doesn't newly match any pattern inherits the most recently
    reached milestone (so a run of several consecutive ``kubectl get pods``
    steps all land in the same milestone, the same way a hand-authored
    contiguous range would) -- until a later pattern fires. Milestones must
    fire in non-decreasing order (by position in the registered list); a
    detector match that would move backwards is ignored rather than silently
    un-reaching an already-passed milestone (this is what makes a later
    re-check of the same resource collapse into "stay where we are" instead
    of incorrectly rewinding the episode).
    """
    detector = _DETECTORS.get(problem_id)
    if detector is None:
        raise KeyError(f"no milestone detector registered for problem_id={problem_id!r}")
    order = {m.id: i for i, m in enumerate(_MILESTONES[problem_id])}

    assigned: dict[int, str] = {}
    current: str | None = None
    reached: set[str] = set()
    for step in steps:
        if step.source != "agent":
            continue
        detected = detector(step, frozenset(reached))
        if detected is not None and (current is None or order[detected] >= order[current]):
            current = detected
            reached.add(detected)
        if current is not None:
            assigned[step.step_id] = current
    return assigned
