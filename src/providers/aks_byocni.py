"""AKS with BYOCNI + Cilium installed by the user. v1 stub."""
from __future__ import annotations

from pathlib import Path

from ..cni import get as get_probe
from ..cni.base import CNIProbe
from .base import ClusterHandle, ClusterProvider


class AKSBYOCNIProvider(ClusterProvider):
    name = "aks_byocni"

    def __init__(self, cfg):
        self.cfg = cfg

    def create(self, cfg) -> ClusterHandle:
        # Placeholder - implement with `az aks create --network-plugin none ...`
        # then `cilium install` (Cilium CLI) or Helm upgrade.
        raise NotImplementedError(
            "aks_byocni.create() not yet implemented. Provision the cluster + Cilium "
            "out-of-band and use --provider existing for now."
        )

    def get_credentials(self, h: ClusterHandle) -> Path:
        raise NotImplementedError

    def delete(self, h: ClusterHandle) -> None:
        raise NotImplementedError

    def node_autoprovision_hint(self) -> dict:
        return {"nodeSelector": {}, "tolerations": []}

    def cni_probe(self) -> CNIProbe:
        return get_probe(self.cfg.cni.probe or "cilium_generic")
