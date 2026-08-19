import json
import logging
import os
import subprocess

import yaml

from sregym.paths import BASE_DIR, BASE_PARENT_DIR, LOKI_METADATA
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl


class Loki:
    def __init__(self):
        self.config_file = LOKI_METADATA
        self.name = None
        self.namespace = None
        self.helm_configs = {}
        self.promtail_release_name = "promtail"
        self.promtail_values_file = str(BASE_DIR / "observer/loki/promtail-values.yaml")
        self.pvc_config_file = None
        self._grafana_repo_dir = BASE_PARENT_DIR / "helm-repos" / "grafana"

        self.logger = logging.getLogger("all.infra.loki")
        self.logger.propagate = True
        self.logger.setLevel(logging.DEBUG)

        self.load_service_json()

    def load_service_json(self):
        """Load Loki service metadata into attributes."""
        with open(self.config_file) as file:
            metadata = json.load(file)

        self.name = metadata.get("Name")
        self.namespace = metadata.get("Namespace")

        self.helm_configs = metadata.get("Helm Config", {})

        self.name = metadata["Name"]
        self.namespace = metadata["Namespace"]
        if "Helm Config" in metadata:
            self.helm_configs = metadata["Helm Config"]
            # Handle remote charts differently - don't prepend BASE_DIR
            if not self.helm_configs.get("remote_chart", False):
                if "chart_path" in self.helm_configs:
                    chart_path = self.helm_configs["chart_path"]
                    self.helm_configs["chart_path"] = str(BASE_DIR / chart_path)

            # Resolve extra_args paths relative to BASE_DIR
            if "extra_args" in self.helm_configs:
                extra_args = self.helm_configs["extra_args"]
                resolved_args = []
                for i, arg in enumerate(extra_args):
                    if i > 0 and extra_args[i - 1] == "-f":
                        # This is a values file path, resolve it
                        resolved_args.append(str(BASE_DIR / arg))
                    else:
                        resolved_args.append(arg)
                self.helm_configs["extra_args"] = resolved_args

        self.pvc_config_file = os.path.join(BASE_DIR, metadata.get("PersistentVolumeClaimConfig"))

    def get_service_json(self) -> dict:
        """Get Loki service metadata in JSON format."""
        with open(self.config_file) as file:
            return json.load(file)

    def get_service_summary(self) -> str:
        """Get a summary of the Loki service metadata."""
        service_json = self.get_service_json()
        service_name = service_json.get("Name", "")
        namespace = service_json.get("Namespace", "")
        desc = service_json.get("Desc", "")
        supported_operations = service_json.get("Supported Operations", [])
        operations_str = "\n".join([f"  - {op}" for op in supported_operations])

        return (
            f"Telemetry Service Name: {service_name}\n"
            f"Namespace: {namespace}\n"
            f"Description: {desc}\n"
            f"Supported Operations:\n{operations_str}"
        )

    def deploy(self):
        """Deploy Loki using Helm."""
        if self._is_loki_running():
            self.logger.warning("Loki is already running. Skipping redeployment.")
            self._deploy_promtail()
            return

        self._delete_pvc()
        Helm.uninstall(**self.helm_configs)

        # Point at the vendored local chart, or add the live Grafana repo.
        self._resolve_loki_chart_source()

        if self.pvc_config_file:
            pvc_name = self._get_pvc_name_from_file(self.pvc_config_file)
            if not self._pvc_exists(pvc_name):
                self._apply_pvc()

        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.namespace)
        self._deploy_promtail()

    def _resolve_loki_chart_source(self):
        """Point Loki's chart_path at the pinned local chart, or fall back
        to the live Grafana Helm repo.

        Helm 3 has no protocol handler for `file://` repos ("could not find
        protocol handler for: file"), so a local repo added that way always
        fails `helm install` with "repo grafana not found" — silently
        breaking Loki/Promtail deploy for every problem, not just ones that
        happen to touch Grafana directly. Referencing the vendored .tgz (see
        deploy/runner/Dockerfile) directly as chart_path sidesteps `helm repo
        add` entirely, since `helm install` can install straight from a
        local chart archive with no repo involved.
        """
        vendored_chart = self._grafana_repo_dir / "loki-7.3.0.tgz"
        if vendored_chart.is_file():
            self.helm_configs["chart_path"] = str(vendored_chart)
            return

        # Outside the runner image (e.g. a dev running main.py directly) the
        # vendored chart won't exist, so fall back to the live repo.
        self.logger.info("Adding Grafana Helm repository...")
        try:
            KubeCtl().exec_command("helm repo add grafana https://grafana.github.io/helm-charts")
            KubeCtl().exec_command("helm repo update")
        except Exception as e:
            self.logger.warning(f"Failed to add Grafana Helm repo (may already exist): {e}")

    def teardown(self):
        """Teardown the Loki deployment."""
        Helm.uninstall(**self.helm_configs)
        Helm.uninstall(release_name=self.promtail_release_name, namespace=self.namespace)

        if self.pvc_config_file:
            self._delete_pvc()

    def _apply_pvc(self):
        """Apply the PersistentVolumeClaim configuration."""
        self.logger.info(f"Applying PersistentVolumeClaim from {self.pvc_config_file}")
        KubeCtl().exec_command(f"kubectl apply -f {self.pvc_config_file} -n {self.namespace}")

    def _delete_pvc(self):
        """Delete the PersistentVolume and associated PersistentVolumeClaim."""
        pvc_name = self._get_pvc_name_from_file(self.pvc_config_file)
        result = KubeCtl().exec_command(f"kubectl get pvc {pvc_name} --ignore-not-found")

        if result:
            self.logger.info(f"Deleting PersistentVolumeClaim {pvc_name}")
            KubeCtl().exec_command(f"kubectl delete pvc {pvc_name}")
            self.logger.info(f"Successfully deleted PersistentVolumeClaim from {pvc_name}")
        else:
            self.logger.warning(f"PersistentVolumeClaim {pvc_name} not found. Skipping deletion.")

    def _get_pvc_name_from_file(self, pv_config_file):
        """Extract PVC name from the configuration file."""
        with open(pv_config_file) as file:
            pv_config = yaml.safe_load(file)
            return pv_config["metadata"]["name"]

    def _pvc_exists(self, pvc_name: str) -> bool:
        """Check if the PersistentVolumeClaim exists."""
        command = f"kubectl get pvc {pvc_name}"
        try:
            result = KubeCtl().exec_command(command)
            if "No resources found" in result or "Error" in result:
                return False
        except subprocess.CalledProcessError:
            return False
        return True

    def _is_loki_running(self) -> bool:
        """Check if Loki is already running in the cluster."""
        command = f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=loki"
        try:
            result = KubeCtl().exec_command(command)
            if "Running" in result:
                return True
        except subprocess.CalledProcessError:
            return False
        return False

    def _deploy_promtail(self):
        if self._is_promtail_running():
            self.logger.warning("Promtail is already running. Skipping redeployment.")
            return

        self.logger.info("Deploying Promtail for Loki log collection...")
        vendored_chart = self._grafana_repo_dir / "promtail-6.16.6.tgz"
        promtail_chart_path = str(vendored_chart) if vendored_chart.is_file() else "grafana/promtail"
        Helm.install(
            release_name=self.promtail_release_name,
            chart_path=promtail_chart_path,
            namespace=self.namespace,
            remote_chart=True,
            extra_args=["-f", self.promtail_values_file],
        )
        Helm.assert_if_deployed(self.namespace)

    def _is_promtail_running(self) -> bool:
        """Check if Promtail is already running in the cluster."""
        command = f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=promtail"
        try:
            result = KubeCtl().exec_command(command)
            if "Running" in result:
                return True
        except subprocess.CalledProcessError:
            return False
        return False
