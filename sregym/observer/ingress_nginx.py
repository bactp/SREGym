import logging
import subprocess
import time

logger = logging.getLogger("all.sregym.ingress_nginx")


class IngressNginx:
    def __init__(self):
        self.namespace = "ingress-nginx"
        self.release_name = "ingress-nginx"

    def run_cmd(self, cmd: str) -> str:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Command failed: {cmd}\nError: {result.stderr}")
        return result.stdout.strip()

    def deploy(self):
        """Deploy the nginx ingress controller via Helm."""
        # Ensure the helm repo is available
        self.run_cmd("helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>/dev/null || true")
        self.run_cmd("helm repo update ingress-nginx")

        # Only claim the default IngressClass when the cluster has none yet —
        # live clusters may already have a platform-managed default ingress
        # controller that other workloads depend on staying default.
        existing_default = self.run_cmd(
            "kubectl get ingressclass -o jsonpath="
            "'{range .items[?(@.metadata.annotations.ingressclass\\.kubernetes\\.io/is-default-class==\"true\")]}"
            '{.metadata.name}{"\\n"}{end}\''
        )
        set_default = not existing_default.strip()
        if not set_default:
            logger.info(
                f"Existing default IngressClass detected ({existing_default.strip()}); "
                "leaving it as default and skipping ingress-nginx default flag."
            )

        # Install or upgrade the chart
        self.run_cmd(
            f"helm upgrade --install {self.release_name} ingress-nginx/ingress-nginx "
            f"--namespace {self.namespace} --create-namespace "
            "--set controller.service.type=ClusterIP "
            f"--set controller.ingressClassResource.default={'true' if set_default else 'false'} "
            "--set controller.watchIngressWithoutClass=true"
        )
        self._wait_for_ready(timeout=120)
        logger.info("Nginx ingress controller deployed successfully.")

    def _wait_for_ready(self, timeout: int = 120):
        """Wait until the ingress controller deployment is ready."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                out = self.run_cmd(
                    f"kubectl -n {self.namespace} get deployment ingress-nginx-controller "
                    f"-o jsonpath='{{.status.readyReplicas}}'"
                )
                if out.strip("'") == "1":
                    return
            except Exception:
                pass
            time.sleep(3)
        raise RuntimeError(f"Nginx ingress controller not ready within {timeout}s")
