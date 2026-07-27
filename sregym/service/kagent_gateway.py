import logging
import os
import socket
import subprocess
import time

import requests

logger = logging.getLogger("all.sregym.kagent_gateway")

DEFAULT_MGMT_KUBECONFIG = os.path.expanduser("~/mgmt.kubeconfig")


class KagentGateway:
    """Port-forwards kagent's controller Service (on the mgmt cluster) onto the host.

    kagent runs on a separate cluster from SREGym's own target (KUBECONFIG points at the
    fault-injection cluster), so this uses its own kubeconfig (KAGENT_KUBECONFIG env var,
    defaulting to ~/mgmt.kubeconfig) on every kubectl invocation rather than the process-wide
    KUBECONFIG/~/.kube/config that the rest of SREGym relies on.
    """

    def __init__(self, namespace: str = "kagent", service_name: str = "kagent-controller", port: int = 8083):
        self.namespace = namespace
        self.service_name = service_name
        self.port = port
        self.kubeconfig = os.environ.get("KAGENT_KUBECONFIG", DEFAULT_MGMT_KUBECONFIG)
        self.port_forward_process = None

    def is_port_in_use(self, port: int) -> bool:
        """Check if a local TCP port is already bound."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _is_port_forward_healthy(self) -> bool:
        """Check if the port-forward is actually serving traffic, not just bound."""
        try:
            resp = requests.get(f"http://127.0.0.1:{self.port}/", timeout=5)
            resp.close()
            # The controller has no route at "/", but any HTTP response (e.g. 404)
            # means something is listening and answering on the other end.
            return resp.status_code < 500
        except Exception:
            return False

    def _kill_stale_port_forward(self):
        """Kill any existing kubectl port-forward process on our port."""
        if self.port_forward_process and self.port_forward_process.poll() is None:
            logger.info("Killing existing port-forward process to re-establish fresh connection.")
            self.stop_port_forward()
            self.port_forward_process = None

        if self.is_port_in_use(self.port):
            try:
                result = subprocess.run(f"lsof -ti tcp:{self.port}", shell=True, capture_output=True, text=True)
                for pid in result.stdout.strip().split():
                    if pid.isdigit():
                        logger.info(f"Killing orphaned process {pid} on port {self.port}")
                        subprocess.run(f"kill {pid}", shell=True)
                time.sleep(1)
            except Exception as e:
                logger.warning(f"Failed to kill stale port-forward: {e}")

    def ensure_started(self):
        """Start the port-forward to kagent-controller if it isn't already up and healthy."""
        if self._is_port_forward_healthy():
            logger.info(f"kagent-controller port-forward already healthy on {self.port}.")
            return

        self._kill_stale_port_forward()

        for attempt in range(3):
            if self.is_port_in_use(self.port):
                logger.debug(f"Port {self.port} in use, waiting to retry ({attempt + 1}/3)...")
                time.sleep(3)
                continue

            command = (
                f"kubectl --kubeconfig {self.kubeconfig} port-forward "
                f"svc/{self.service_name} {self.port}:{self.port} -n {self.namespace} --address 127.0.0.1"
            )
            self.port_forward_process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(4)

            if self.port_forward_process.poll() is None and self._is_port_forward_healthy():
                logger.info(f"kagent-controller port-forward established on {self.port}.")
                return
            logger.warning("kagent-controller port-forward failed to come up healthy, retrying...")

        raise RuntimeError("Failed to establish kagent-controller port-forward after multiple attempts.")

    def stop_port_forward(self):
        """Stop the kubectl port-forward process and clean up resources."""
        if self.port_forward_process:
            self.port_forward_process.terminate()
            try:
                self.port_forward_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Port-forward process did not terminate in time, killing...")
                self.port_forward_process.kill()

            if self.port_forward_process.stdout:
                self.port_forward_process.stdout.close()
            if self.port_forward_process.stderr:
                self.port_forward_process.stderr.close()

            logger.info("Port forwarding for kagent-controller stopped.")
