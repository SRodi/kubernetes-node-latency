"""Use the current kubeconfig context — no cluster lifecycle ops."""
from __future__ import annotations

import os
from pathlib import Path

from ..cni import get as get_probe
from ..cni.base import CNIProbe
from .base import ClusterHandle, ClusterProvider


class ExistingProvider(ClusterProvider):
    name = "existing"

    def __init__(self, cfg):
        self.cfg = cfg

    def create(self, cfg) -> ClusterHandle:
        kc = Path(os.environ.get("KUBECONFIG") or Path.home() / ".kube" / "config")
        return ClusterHandle(name=cfg.cluster_name or "existing",
                             region=cfg.region or "n/a",
                             provider=self.name, kubeconfig=kc, created=False)

    def get_credentials(self, h: ClusterHandle) -> Path:
        return h.kubeconfig

    def delete(self, h: ClusterHandle) -> None:
        return None

    def node_autoprovision_hint(self) -> dict:
        return {"nodeSelector": {}, "tolerations": []}

    def cni_probe(self) -> CNIProbe:
        # Default to generic Cilium; user can override via cfg.cni.probe.
        name = self.cfg.cni.probe or "cilium_generic"
        return get_probe(name)
