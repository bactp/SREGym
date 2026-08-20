"""
KAgent driver for SREGym.
Entry point for running a KAgent-hosted agent (kagent-dev/kagent) on SREGym tasks.

Unlike the CLI-based drivers (codex/claudecode/opencode), the actual agent doesn't run
in this process or even on this host: it runs as a kagent Agent CRD on a separate
management cluster, wired up (via RemoteMCPServer CRDs) to SREGym's own kubectl/
prometheus/jaeger/submit MCP tool servers. This driver's job is just to kick off a
`kagent invoke` call with the task instructions and wait for the conductor to report
the run as done.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
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


def _reconstruct_history_from_stream(stdout: str) -> dict | None:
    """Reassemble a non-streaming-shaped ``{"history": [...]}`` result from ``--stream``
    NDJSON output, so the existing ATIF adapter (``atif_converter/adapters/kagent.py``,
    which expects the single-shot ``kagent invoke`` response shape) can still convert it
    and populate ``Metrics.*``.

    Each NDJSON line is one event. A ``status-update`` event's ``status.message`` is one
    A2A history item (``role``/``parts``/``metadata`` incl. per-turn
    ``kagent_usage_metadata``) - the same shape a non-streaming ``history[]`` entry has.
    The message just before a terminal state is re-emitted verbatim in the final event too,
    so dedupe by ``messageId`` (keep the first occurrence).

    The agent's last turn is typically published *twice*: once as a normal message (with its
    own usage) and again as an ``artifact-update`` carrying identical text with no usage of
    its own, followed by a trailing usage-only ``status-update`` restating that same turn's
    usage. Skip an ``artifact-update`` whose text matches the immediately preceding history
    entry (the common case) rather than double-counting that turn; only keep it as a new
    entry - and reattach the trailing usage-only event to it (FIFO) - when its text genuinely
    doesn't match anything already recorded.
    """
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

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


def _track_pending_tool_calls(ndjson_line: str, pending: set[str]) -> None:
    """Update ``pending`` (call ids awaiting a response) from one ``--stream`` NDJSON
    line. Mirrors the ``status-update``/``message``/``parts`` shape that
    ``_reconstruct_history_from_stream`` parses -- a part with ``name``+``args``+``id``
    and no ``response`` is a function call; a part with a matching ``id``+``response``
    resolves it."""
    try:
        event = json.loads(ndjson_line)
    except json.JSONDecodeError:
        return
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
    idle_timeout_seconds: int = 90,
) -> tuple[int, str, str, bool]:
    """Shell out to `kagent invoke --stream`, reading its NDJSON output line by line.

    Returns (returncode, stdout, stderr, truncated). ``truncated`` is True when the
    stream ends - whether the process exits on its own (even with returncode 0) or
    goes idle past ``idle_timeout_seconds`` - while a tool call is still awaiting its
    response. kagent-controller has been observed to end a task early (e.g. hitting a
    RemoteMCPServer's own timeout mid tool-call) and still report the invoke as a
    normal completion; catching that here lets main() retry immediately instead of
    relying on wait_for_run_done()'s much longer timeout to notice nothing was ever
    submitted.
    """
    command = [
        "kagent",
        "invoke",
        "--agent",
        agent,
        "-n",
        namespace,
        "--kagent-url",
        kagent_url,
        "--task",
        task,
        "--timeout",
        f"{timeout_seconds}s",
        "--output-format",
        "json",
        "--stream",
    ]
    logger.info(f"Invoking kagent agent '{agent}' (timeout={timeout_seconds}s)...")

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    lines: list[str] = []
    stderr_chunks: list[str] = []
    pending_calls: set[str] = set()
    last_line_time = time.time()

    def _read_stdout() -> None:
        nonlocal last_line_time
        for raw_line in process.stdout:
            last_line_time = time.time()
            line = raw_line.rstrip("\n")
            lines.append(line)
            _track_pending_tool_calls(line, pending_calls)

    def _read_stderr() -> None:
        for raw_line in process.stderr:
            stderr_chunks.append(raw_line)

    stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    deadline = time.time() + timeout_seconds + 30
    killed_reason = None
    while stdout_thread.is_alive():
        stdout_thread.join(timeout=1)
        now = time.time()
        if now > deadline:
            killed_reason = f"exceeded overall timeout of {timeout_seconds + 30}s"
            break
        if pending_calls and (now - last_line_time) > idle_timeout_seconds:
            killed_reason = f"stalled {idle_timeout_seconds}s with pending tool call(s) {pending_calls}"
            break

    if killed_reason:
        logger.warning(f"kagent invoke {killed_reason}; killing subprocess")
        process.kill()

    stdout_thread.join(timeout=10)
    stderr_thread.join(timeout=10)
    returncode = process.wait(timeout=10)

    stdout = "\n".join(lines)
    stderr = "".join(stderr_chunks)
    truncated = bool(pending_calls) or killed_reason is not None
    return returncode, stdout, stderr, truncated


def main():
    parser = argparse.ArgumentParser(description="Run a KAgent agent on SREGym tasks")
    parser.add_argument("--kagent-agent", default=os.environ.get("KAGENT_AGENT_NAME", "sre-agent"))
    parser.add_argument("--kagent-namespace", default=os.environ.get("KAGENT_NAMESPACE", "kagent"))
    parser.add_argument("--kagent-port", type=int, default=int(os.environ.get("KAGENT_CONTROLLER_PORT", "8083")))
    parser.add_argument("--invoke-timeout", type=int, default=int(os.environ.get("KAGENT_INVOKE_TIMEOUT", "3600")))
    parser.add_argument(
        "--invoke-retries",
        type=int,
        default=int(os.environ.get("KAGENT_INVOKE_RETRIES", "1")),
        help="Extra attempts if kagent invoke's stream ends on an unanswered tool call.",
    )
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
    max_attempts = args.invoke_retries + 1
    returncode, parsed_result = 1, None
    for attempt in range(1, max_attempts + 1):
        try:
            returncode, stdout, stderr, truncated = invoke_kagent(
                agent=args.kagent_agent,
                namespace=args.kagent_namespace,
                kagent_url=kagent_url,
                task=instruction,
                timeout_seconds=args.invoke_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error("kagent invoke subprocess timed out")
            returncode, stdout, stderr, truncated = 1, "", "subprocess timeout", True

        if returncode != 0:
            logger.warning(f"kagent invoke exited with code {returncode}. stderr: {stderr[:2000]}")

        parsed_result = _reconstruct_history_from_stream(stdout)
        if parsed_result is None:
            try:
                parsed_result = json.loads(stdout)
            except json.JSONDecodeError:
                logger.warning("Could not parse kagent invoke output as NDJSON stream or plain JSON, saving raw text instead")
                parsed_result = stdout

        result_label = problem_id if attempt == 1 else f"{problem_id}_attempt{attempt}"
        save_invocation_result(logs_dir, result_label, parsed_result if isinstance(parsed_result, dict) else stdout)

        if not truncated:
            break
        if attempt < max_attempts:
            logger.warning(
                f"kagent invoke result ended on an unanswered tool call (attempt {attempt}/{max_attempts}); retrying..."
            )
        else:
            logger.error(
                f"kagent invoke result still truncated after {max_attempts} attempt(s); "
                "giving up and letting conductor's stage-timeout report the run as incomplete"
            )

    final_stage = wait_for_run_done(timeout=args.done_wait_timeout)

    logger.info("=" * 80)
    logger.info(f"KAgent driver finished. kagent invoke exit={returncode}, final conductor stage={final_stage!r}")
    logger.info("=" * 80)

    sys.exit(0 if final_stage == "done" else 1)


if __name__ == "__main__":
    main()
