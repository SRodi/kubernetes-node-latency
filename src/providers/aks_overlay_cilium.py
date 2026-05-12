"""AKS with Azure CNI Powered by Cilium (managed dataplane)."""
from __future__ import annotations

from ._aks_base import AKSProviderBase


class AKSOverlayCiliumProvider(AKSProviderBase):
    name = "aks_overlay_cilium"

    def _az_create_cluster_args(self, cfg) -> list[str]:
        return [
            "--network-plugin", "azure",
            "--network-plugin-mode", "overlay",
            "--network-dataplane", "cilium",
            "--pod-cidr", "10.244.0.0/16",
        ]

    def _network_describe(self) -> dict:
        return {
            "network_plugin": "azure",
            "network_plugin_mode": "overlay",
            "network_dataplane": "cilium",
        }
