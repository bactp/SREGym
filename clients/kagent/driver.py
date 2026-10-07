"""
KAgent driver for SREGym.
Entry point for running a KAgent-hosted agent (kagent-dev/kagent) on SREGym tasks.

Unlike the CLI-based drivers (codex/claudecode/opencode), the actual agent doesn't run
in this process or even on this host: it runs as a kagent Agent CRD on a separate
management cluster, wired up (via RemoteMCPServer CRDs) to SREGym's own kubectl/
prometheus/jaeger/submit MCP tool servers. This driver's job is just to kick off a
task on it and wait for the conductor to report the run as done.

Talks to kagent-controller's A2A endpoint (``POST /api/a2a/<namespace>/<agent>/``,
JSON-RPC 2.0 ``message/stream``) directly over HTTP instead of shelling out to the
`kagent` CLI's `invoke` command. Two bugs in that CLI (present through at least
v0.10.0-rc1, both still open upstream) make it unsuitable as of this writing:
  - Every error path in `invoke` prints to stderr and does a bare `return`, never
    `os.Exit(1)` - so a failed invoke still reports exit code 0 to a caller checking
    returncode (`go/core/cli/internal/cli/agent/invoke.go`).
  - Both `invoke --stream` and non-streaming `invoke` wrap the actual A2A call in a
    hardcoded `context.WithTimeout(ctx, 300*time.Second)` that silently caps
    `--timeout` at 300s no matter what value is passed (same file).
The wire format itself (JSON-RPC 2.0 over SSE) was captured directly off a real
`kagent invoke --stream` run via a local proxy, not guessed from the spec.
"""

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

# Add SREGym root to path
sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from logger import init_logger  # noqa: E402

init_logger()

from clients.harness.problem_id import resolve_problem_id  # noqa: E402
from sregym.service.kagent_gateway import KagentGateway  # noqa: E402

logger = logging.getLogger("all.kagent.driver")


def get_api_base_url() -> str:
    """Get the conductor API base URL."""
    host = os.getenv("API_HOSTNAME", "localhost")
    port = os.getenv("API_PORT", "8000")
    return f"http://{host}:{port}"


def get_app_info() -> dict:
    """Get application info from conductor API."""
    api_url = f"{get_api_base_url()}/get_app"
    logger.info(f"Fetching app info from {api_url}")

    response = requests.get(api_url)
    response.raise_for_status()
    app_info = response.json()
    logger.info(f"App info: {app_info}")
    return app_info


def get_stage(timeout: int = 5) -> str | None:
    """Fetch the conductor's current stage, or None on a transient error."""
    try:
        response = requests.get(f"{get_api_base_url()}/status", timeout=timeout)
        response.raise_for_status()
        return response.json().get("stage")
    except Exception as e:
        logger.debug(f"Error checking status: {e}")
        return None


def wait_for_ready_stage(timeout: int = 300) -> str:
    """Wait for conductor to reach a submission-ready stage (diagnosis or mitigation)."""
    allowed_stages = {"diagnosis", "mitigation"}
    start_time = time.time()

    logger.info("Waiting for conductor to reach submission-ready stage...")
    while time.time() - start_time < timeout:
        stage = get_stage()
        if stage in allowed_stages:
            logger.info(f"Conductor ready at stage: {stage}")
            return stage
        time.sleep(1)

    raise TimeoutError(f"Conductor did not reach ready stage within {timeout} seconds")


def wait_for_run_done(timeout: int) -> str | None:
    """Poll conductor /status until it reports "done" (or a terminal-looking stage).

    This is the authoritative completion signal SREGym's other clients rely on too -
    kagent invoke returning doesn't guarantee the conductor has finished evaluating the
    last submission yet (evaluation runs in a background thread server-side).
    """
    start_time = time.time()
    last_stage = None
    while time.time() - start_time < timeout:
        stage = get_stage()
        if stage is not None:
            last_stage = stage
        if stage == "done":
            return stage
        time.sleep(2)
    logger.warning(f"Timed out waiting for conductor to report done (last seen stage: {last_stage!r})")
    return last_stage


def build_instruction(app_info: dict) -> str:
    """Build the task instruction string for the kagent-hosted sre-agent."""
    app_name = app_info.get("app_name", "unknown")
    namespace = app_info.get("namespace", "default")
    descriptions = app_info.get("descriptions", "")

    return f"""You are diagnosing and fixing an issue in a Kubernetes application.

Application: {app_name}
Namespace: {namespace}

{descriptions}

Follow the DIAGNOSIS then MITIGATION workflow and submission rules from your system prompt.
Work autonomously from here - do not ask for confirmation."""


def _reconstruct_history_from_events(events: list[dict]) -> dict | None:
    """Reassemble a non-streaming-shaped ``{"history": [...]}`` result from a sequence of
    parsed A2A ``message/stream`` events (the ``result`` field of each SSE frame's JSON-RPC
    envelope), so the existing ATIF adapter (``atif_converter/adapters/kagent.py``, which
    expects the single-shot ``kagent invoke`` response shape) can still convert it and
    populate ``Metrics.*``.

    A ``status-update`` event's ``status.message`` is one A2A history item
    (``role``/``parts``/``metadata`` incl. per-turn ``kagent_usage_metadata``) - the same
    shape a non-streaming ``history[]`` entry has. The message just before a terminal state
    is re-emitted verbatim in the final event too, so dedupe by ``messageId`` (keep the first
    occurrence).

    The agent's last turn is typically published *twice*: once as a normal message (with its
    own usage) and again as an ``artifact-update`` carrying identical text with no usage of
    its own, followed by a trailing usage-only ``status-update`` restating that same turn's
    usage. Skip an ``artifact-update`` whose text matches the immediately preceding history
    entry (the common case) rather than double-counting that turn; only keep it as a new
    entry - and reattach the trailing usage-only event to it (FIFO) - when its text genuinely
    doesn't match anything already recorded.
    """
    context_id = None
    history: list[dict] = []
    seen_message_ids: set[str] = set()
    pending_artifact_indices: list[int] = []

    for event in events:
        context_id = event.get("contextId", context_id)
        kind = event.get("kind")

        if kind == "status-update":
            message = (event.get("status") or {}).get("message")
            usage = (event.get("metadata") or {}).get("kagent_usage_metadata")
            if message:
                message_id = message.get("messageId")
                if message_id and message_id in seen_message_ids:
                    continue
                if message_id:
                    seen_message_ids.add(message_id)
                history.append(message)
            elif usage and pending_artifact_indices:
                target = pending_artifact_indices.pop(0)
                history[target].setdefault("metadata", {})["kagent_usage_metadata"] = usage
        elif kind == "artifact-update":
            parts = (event.get("artifact") or {}).get("parts") or []
            if not parts:
                continue
            artifact_text = "".join(p.get("text", "") for p in parts if p.get("kind") == "text")
            if history:
                last_parts = history[-1].get("parts", [])
                last_text = "".join(p.get("text", "") for p in last_parts if p.get("kind") == "text")
                if artifact_text and artifact_text == last_text:
                    continue  # redundant restatement of the turn just added above
            history.append({"role": "agent", "parts": parts, "metadata": {}})
            pending_artifact_indices.append(len(history) - 1)

    if not history:
        return None

    return {"contextId": context_id, "history": history}


def save_invocation_result(logs_dir: Path, problem_id: str, result: dict | str) -> Path:
    """Best-effort dump of the raw kagent invoke output, for parity with other clients' logs."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%m%d_%H%M")
    out_path = logs_dir / f"{timestamp}_{problem_id}_kagent_invoke_result.json"
    with open(out_path, "w", encoding="utf-8") as f:
        if isinstance(result, str):
            f.write(result)
        else:
            json.dump(result, f, indent=2)
    logger.info(f"Saved kagent invoke result to {out_path}")
    return out_path


def _track_pending_tool_calls(event: dict, pending: set[str]) -> None:
    """Update ``pending`` (call ids awaiting a response) from one parsed ``message/stream``
    event. Mirrors the ``status-update``/``message``/``parts`` shape that
    ``_reconstruct_history_from_events`` parses -- a part with ``name``+``args``+``id``
    and no ``response`` is a function call; a part with a matching ``id``+``response``
    resolves it."""
    if event.get("kind") != "status-update":
        return
    message = (event.get("status") or {}).get("message") or {}
    for part in message.get("parts", []):
        if part.get("kind") != "data":
            continue
        data = part.get("data", {})
        call_id = data.get("id")
        if not call_id:
            continue
        if "response" in data:
            pending.discard(call_id)
        elif "name" in data and "args" in data:
            pending.add(call_id)


def invoke_kagent(
    agent: str,
    namespace: str,
    kagent_url: str,
    task: str,
    timeout_seconds: int,
) -> tuple[list[dict], bool, str | None]:
    """Call kagent-controller's A2A endpoint directly: ``POST /api/a2a/<namespace>/<agent>/``,
    JSON-RPC 2.0 method ``message/stream``, reading the ``text/event-stream`` (SSE) response
    one event at a time. This is what the `kagent invoke --stream` CLI command does
    internally - captured directly off a real invocation via a local proxy - minus the two
    CLI bugs noted in the module docstring.

    Returns (events, truncated, task_id). ``events`` is the list of parsed A2A event objects
    (the ``result`` field of each JSON-RPC/SSE frame) received before the stream ended.
    ``truncated`` is True when the stream ends - cleanly or not - while a tool call is still
    awaiting its response, or when our own ``timeout_seconds`` deadline is hit first.
    kagent-controller has been observed to end a task early and still report success;
    ``truncated`` just lets main() log that clearly instead of trusting a clean-looking
    stream end. main() deliberately does NOT retry on this - see its comment for why a
    client-side retry here is unsafe.

    Deliberately does NOT try to cancel the task server-side when ``truncated`` - the A2A
    ``tasks/cancel`` method is wired up in kagent-controller's passthrough handler, but the
    Declarative/ADK Python agent runtime backing every SREGym agent returns
    ``Cancellation is not supported`` for it (confirmed by calling it directly against a
    live task). The task keeps running server-side regardless of what this function does.
    """
    url = f"{kagent_url.rstrip('/')}/api/a2a/{namespace}/{agent}/"
    request_id = str(uuid.uuid4())
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/stream",
        "params": {
            "message": {
                "kind": "message",
                "messageId": "",
                "parts": [{"kind": "text", "text": task}],
                "role": "user",
            }
        },
    }
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "text/event-stream"}

    logger.info(f"Invoking kagent agent '{agent}' via A2A (timeout={timeout_seconds}s)...")

    events: list[dict] = []
    pending_calls: set[str] = set()
    task_id: str | None = None
    deadline_exceeded = False
    deadline = time.time() + timeout_seconds

    try:
        # read timeout is a hard backstop against a truly hung read() call (mirrors the
        # +30s grace this replaced) - the real ceiling is the deadline checked per-event
        # below, since a legitimately busy agent can go quiet between SSE frames for a
        # while with no real stall (kagent-controller's own a2aClientTimeout, set via Helm,
        # is what would otherwise cut this off server-side - see module docstring).
        response = requests.post(url, json=body, headers=headers, stream=True, timeout=(10, timeout_seconds + 30))
        with response:
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=True):
                if time.time() > deadline:
                    deadline_exceeded = True
                    break
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                try:
                    envelope = json.loads(raw_line[len("data:") :].strip())
                except json.JSONDecodeError:
                    continue
                if "error" in envelope:
                    logger.warning(f"kagent A2A stream returned an error: {envelope['error']}")
                    break
                event = envelope.get("result")
                if not isinstance(event, dict):
                    continue
                events.append(event)
                task_id = event.get("taskId", task_id)
                _track_pending_tool_calls(event, pending_calls)
                if event.get("final"):
                    break
    except requests.exceptions.RequestException as e:
        logger.warning(f"kagent A2A request failed: {e}")

    if deadline_exceeded:
        logger.warning(f"kagent A2A call exceeded overall timeout of {timeout_seconds}s")

    truncated = bool(pending_calls) or deadline_exceeded
    return events, truncated, task_id


def main():
    parser = argparse.ArgumentParser(description="Run a KAgent agent on SREGym tasks")
    parser.add_argument("--kagent-agent", default=os.environ.get("KAGENT_AGENT_NAME", "sre-agent"))
    parser.add_argument("--kagent-namespace", default=os.environ.get("KAGENT_NAMESPACE", "kagent"))
    parser.add_argument("--kagent-port", type=int, default=int(os.environ.get("KAGENT_CONTROLLER_PORT", "8083")))
    parser.add_argument("--invoke-timeout", type=int, default=int(os.environ.get("KAGENT_INVOKE_TIMEOUT", "3600")))
    parser.add_argument("--done-wait-timeout", type=int, default=120)
    parser.add_argument(
        "--logs-dir", type=str, default=os.environ.get("AGENT_LOGS_DIR", "./logs/kagent")
    )
    parser.add_argument("--problem-id", type=str, default=None)
    args = parser.parse_args()

    logger.info("=" * 80)
    logger.info("Starting KAgent driver for SREGym")
    logger.info(f"kagent agent: {args.kagent_agent} (namespace={args.kagent_namespace})")
    logger.info("=" * 80)

    problem_id = resolve_problem_id(cli_problem_id=args.problem_id)
    logger.info(f"Problem ID (harness): {problem_id}")

    kagent_url = os.environ.get("KAGENT_CONTROLLER_URL")
    if kagent_url:
        logger.info(f"Using KAGENT_CONTROLLER_URL override, skipping port-forward: {kagent_url}")
    else:
        gateway = KagentGateway(namespace=args.kagent_namespace, port=args.kagent_port)
        gateway.ensure_started()
        kagent_url = f"http://127.0.0.1:{args.kagent_port}"

    try:
        stage = wait_for_ready_stage(timeout=300)
        logger.info(f"Conductor is ready at stage: {stage}")
    except TimeoutError as e:
        logger.error(f"Timeout waiting for conductor: {e}")
        sys.exit(1)

    try:
        app_info = get_app_info()
    except Exception as e:
        logger.error(f"Failed to get app info: {e}")
        sys.exit(1)

    instruction = build_instruction(app_info)

    logs_dir = Path(args.logs_dir)
    events, truncated, task_id = invoke_kagent(
        agent=args.kagent_agent,
        namespace=args.kagent_namespace,
        kagent_url=kagent_url,
        task=instruction,
        timeout_seconds=args.invoke_timeout,
    )

    parsed_result = _reconstruct_history_from_events(events)
    if parsed_result is None:
        logger.warning("Got no usable history out of the A2A event stream, saving raw events instead")
        parsed_result = {"taskId": task_id, "raw_events": events}

    save_invocation_result(logs_dir, problem_id, parsed_result)

    if truncated:
        # Deliberately NOT retried: kagent-controller's task keeps running server-side
        # independent of this client's HTTP call, so re-invoking here would race a
        # second, genuinely concurrent session against the one that (for all we know)
        # is still working - and if the original later submits, its real trajectory
        # would be clobbered by whatever this discarded retry happened to produce. Also
        # NOT cancelled - see invoke_kagent()'s docstring, tasks/cancel is unsupported by
        # the agent runtime. The safe move is to just wait: wait_for_run_done() below
        # already polls the conductor's own /status for the authoritative completion
        # signal, so if the in-flight invoke was merely reported early (not actually
        # dead), it'll still be picked up as "done" once it really finishes.
        logger.warning(
            f"kagent A2A stream ended on an unanswered tool call (taskId={task_id!r}) - the task may "
            "still be running server-side on kagent-controller. Not retrying (would race a duplicate "
            "session) and not cancelling (unsupported by the agent runtime); waiting on the "
            "conductor's own completion signal instead."
        )

    final_stage = wait_for_run_done(timeout=args.done_wait_timeout)

    logger.info("=" * 80)
    logger.info(f"KAgent driver finished. taskId={task_id!r}, final conductor stage={final_stage!r}")
    logger.info("=" * 80)

    sys.exit(0 if final_stage == "done" else 1)


if __name__ == "__main__":
    main()
