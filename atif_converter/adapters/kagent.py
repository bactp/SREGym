"""KAgent (kagent-dev/kagent) -> ATIF v1.7 adapter.

Converts the raw JSON returned by ``kagent invoke --output-format json`` (as saved by
``clients/kagent/driver.py:save_invocation_result``) into an ATIF ``Trajectory``.

Input shape (A2A protocol task object): a top-level ``history`` array of messages. Each
message has ``role`` (``"user"`` or ``"agent"``) and ``parts``. An agent message that
decided to call a tool has a single ``data``-kind part tagged
``metadata.kagent_type == "function_call"`` and carries the LLM's
``metadata.kagent_usage_metadata`` (``promptTokenCount``/``candidatesTokenCount``/
``totalTokenCount``) for that turn. The tool's result arrives as the *next* history item,
a ``data``-kind part tagged ``metadata.kagent_type == "function_response"`` with no usage
metadata (it isn't an LLM call). A final wrap-up turn (after all necessary ``submit`` calls
already happened) is a plain ``text``-kind part, still carrying usage metadata.

Deliberate simplifications (matching our other adapters):

- No cache-hit accounting: kagent normalizes usage into a Gemini-shaped schema
  (``candidatesTokenCount``/``promptTokenCount``/``totalTokenCount``) regardless of the
  underlying provider, and does not surface a cached/prefill-token field even when the
  real provider (e.g. OpenAI) would have reported one. ``cached_tokens`` is left ``None``.
- No cost computation, same as ``codex``/``gemini`` adapters. ``total_cost_usd`` is
  left ``None``.
- No per-step timestamps: the synchronous ``kagent invoke`` response carries no per-turn
  wall-clock time, only a single top-level ``status.timestamp``. ``Step.timestamp`` is
  left unset.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..atif import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "kagent"


def _extract_text(parts: list[dict[str, Any]]) -> str:
    """Join all text-kind parts of a history item into one string."""
    return "\n".join(p.get("text", "") for p in parts if p.get("kind") == "text" and p.get("text"))


def _dedupe_consecutive_user_echoes(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive identical ``role: "user"`` entries.

    The A2A history we've observed echoes the initial task instruction as two back-to-back
    identical user messages. Only collapse when adjacent and byte-identical, so genuinely
    distinct multi-turn user input is never dropped.
    """
    result: list[dict[str, Any]] = []
    prev_user_text: str | None = None
    for item in history:
        if item.get("role") == "user":
            text = _extract_text(item.get("parts", []))
            if text == prev_user_text:
                continue
            prev_user_text = text
        else:
            prev_user_text = None
        result.append(item)
    return result


def _observation_text(response_data: dict[str, Any]) -> str | None:
    content = response_data.get("response", {}).get("content", [])
    if not isinstance(content, list):
        return None
    texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("text")]
    return "\n".join(texts) or None


def _metrics_from_usage(usage: dict[str, Any] | None) -> Metrics | None:
    if not usage:
        return None
    return Metrics(
        prompt_tokens=usage.get("promptTokenCount"),
        completion_tokens=usage.get("candidatesTokenCount"),
        cached_tokens=None,
    )


def _convert(kagent_result: dict[str, Any]) -> Trajectory | None:
    history = kagent_result.get("history", [])
    if not history:
        return None
    history = _dedupe_consecutive_user_echoes(history)

    steps: list[Step] = []
    step_id = 1
    total_prompt = 0
    total_completion = 0

    i = 0
    n = len(history)
    while i < n:
        item = history[i]
        role = item.get("role")
        parts = item.get("parts", [])
        part = parts[0] if parts else {}
        part_kind = part.get("kind")
        part_meta = part.get("metadata", {}) or {}

        if role == "user":
            steps.append(Step(step_id=step_id, source="user", message=_extract_text(parts)))
            step_id += 1
            i += 1
            continue

        if role != "agent":
            i += 1
            continue

        meta = item.get("metadata", {}) or {}
        usage = meta.get("kagent_usage_metadata")
        metrics = _metrics_from_usage(usage)
        if usage:
            total_prompt += usage.get("promptTokenCount") or 0
            total_completion += usage.get("candidatesTokenCount") or 0

        if part_kind == "data" and part_meta.get("kagent_type") == "function_call":
            data = part.get("data", {})
            tool_call_id = data.get("id") or f"call_{step_id}"
            tool_calls = [
                ToolCall(
                    tool_call_id=tool_call_id,
                    function_name=data.get("name", ""),
                    arguments=data.get("args", {}) or {},
                )
            ]

            observation: Observation | None = None
            if i + 1 < n:
                nxt = history[i + 1]
                nxt_parts = nxt.get("parts", [])
                nxt_part = nxt_parts[0] if nxt_parts else {}
                if nxt.get("role") == "agent" and nxt_part.get("metadata", {}).get("kagent_type") == "function_response":
                    resp_text = _observation_text(nxt_part.get("data", {}))
                    observation = Observation(
                        results=[ObservationResult(source_call_id=tool_call_id, content=resp_text)]
                    )
                    i += 1  # consume the paired response too

            steps.append(
                Step(
                    step_id=step_id,
                    source="agent",
                    message="",
                    tool_calls=tool_calls,
                    observation=observation,
                    metrics=metrics,
                    llm_call_count=1 if metrics else 0,
                )
            )
            step_id += 1
            i += 1
            continue

        if part_kind == "text":
            steps.append(
                Step(
                    step_id=step_id,
                    source="agent",
                    message=part.get("text", ""),
                    metrics=metrics,
                    llm_call_count=1 if metrics else 0,
                )
            )
            step_id += 1
            i += 1
            continue

        # Unrecognized part shape (e.g. an orphaned function_response with no
        # preceding call) - skip rather than guess.
        i += 1

    if not steps:
        return None

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=kagent_result.get("contextId"),
        agent=Agent(name=AGENT_NAME, version="unknown"),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=total_prompt or None,
            total_completion_tokens=total_completion or None,
            total_cached_tokens=None,
            total_steps=len(steps),
        ),
    )


def convert_file(session_file: Path | str) -> Trajectory | None:
    """Convert one saved ``kagent invoke`` JSON result file to ATIF."""
    session_file = Path(session_file)
    try:
        kagent_result = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Could not read kagent invoke result at %s: %s", session_file, exc)
        return None
    if not isinstance(kagent_result, dict):
        return None
    return _convert(kagent_result)
