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


def invoke_kagent(
    agent: str,
    namespace: str,
    kagent_url: str,
    task: str,
    timeout_seconds: int,
) -> tuple[int, str, str]:
    """Shell out to `kagent invoke`. Returns (returncode, stdout, stderr)."""
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
    ]
    logger.info(f"Invoking kagent agent '{agent}' (timeout={timeout_seconds}s)...")
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 30,
    )
    return result.returncode, result.stdout, result.stderr


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
    try:
        returncode, stdout, stderr = invoke_kagent(
            agent=args.kagent_agent,
            namespace=args.kagent_namespace,
            kagent_url=kagent_url,
            task=instruction,
            timeout_seconds=args.invoke_timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("kagent invoke subprocess timed out")
        returncode, stdout, stderr = 1, "", "subprocess timeout"

    if returncode != 0:
        logger.warning(f"kagent invoke exited with code {returncode}. stderr: {stderr[:2000]}")

    parsed_result = stdout
    try:
        parsed_result = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("Could not parse kagent invoke output as JSON, saving raw text instead")

    save_invocation_result(logs_dir, problem_id, parsed_result if isinstance(parsed_result, dict) else stdout)

    final_stage = wait_for_run_done(timeout=args.done_wait_timeout)

    logger.info("=" * 80)
    logger.info(f"KAgent driver finished. kagent invoke exit={returncode}, final conductor stage={final_stage!r}")
    logger.info("=" * 80)

    sys.exit(0 if final_stage == "done" else 1)


if __name__ == "__main__":
    main()
