"""AKS with Azure CNI Powered by Cilium (managed dataplane). v1 stub."""
from __future__ import annotations

from pathlib import Path

from ..cni import get as get_probe
from ..cni.base import CNIProbe
from .base import ClusterHandle, ClusterProvider


class AKSOverlayCiliumProvider(ClusterProvider):
    name = "aks_overlay_cilium"

    def __init__(self, cfg):
        self.cfg = cfg

    def create(self, cfg) -> ClusterHandle:
        # Placeholder - implement with `az aks create --network-plugin azure
        #   --network-plugin-mode overlay --network-dataplane cilium ...`
        raise NotImplementedError(
            "aks_overlay_cilium.create() not yet implemented. Provision the cluster "
            "out-of-band and use --provider existing for now."
        )

    def get_credentials(self, h: ClusterHandle) -> Path:
        raise NotImplementedError

    def delete(self, h: ClusterHandle) -> None:
        raise NotImplementedError

    def node_autoprovision_hint(self) -> dict:
        # AKS does not autoprovision by default; rely on cluster-autoscaler / NAP.
        return {"nodeSelector": {}, "tolerations": []}

    def cni_probe(self) -> CNIProbe:
        return get_probe(self.cfg.cni.probe or "cilium_generic")
