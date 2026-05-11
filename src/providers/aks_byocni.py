"""AKS with BYOCNI + Cilium installed via Helm chart."""
from __future__ import annotations

import logging

from . import _az
from ._aks_base import AKSProviderBase
from .base import ClusterHandle

log = logging.getLogger(__name__)


class AKSBYOCNIProvider(AKSProviderBase):
    name = "aks_byocni"

    def _az_create_cluster_args(self, cfg) -> list[str]:
        return ["--network-plugin", "none"]

    def _post_create(self, handle: ClusterHandle) -> None:
        b = self.cfg.aks.byocni
        log.info("installing Cilium %s via Helm onto %s", b.cilium_chart_version, handle.name)
        _az.helm_repo_add("cilium", b.cilium_repo_url)
        _az.helm_install_cilium(
            kubeconfig=handle.kubeconfig,
            version=b.cilium_chart_version,
            values=b.cilium_values,
            timeout_s=b.install_timeout_s,
        )
        _az.kubectl_rollout_status(handle.kubeconfig, kind="daemonset",
                                    name="cilium", namespace="kube-system",
                                    timeout_s=b.install_timeout_s)
