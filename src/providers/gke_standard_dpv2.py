"""GKE Standard with Dataplane V2."""
from __future__ import annotations

import os
from pathlib import Path

from ..cni import get as get_probe
from ..cni.base import CNIProbe
from ._cli import run
from .base import ClusterHandle, ClusterProvider


class GKEStandardDPv2Provider(ClusterProvider):
    name = "gke_standard_dpv2"

    def __init__(self, cfg):
        self.cfg = cfg

    def _kubeconfig_path(self, cluster_name: str) -> Path:
        if getattr(self.cfg, "kubeconfig_path", None):
            return Path(self.cfg.kubeconfig_path)
        return Path.cwd() / f".kubeconfig-{self.name}-{cluster_name}"

    def create(self, cfg) -> ClusterHandle:
        kc = self._kubeconfig_path(cfg.cluster_name)
        cmd = [
            "gcloud", "container", "clusters", "create", cfg.cluster_name,
            "--region", cfg.region,
            "--release-channel", cfg.release_channel,
            "--enable-dataplane-v2",
            "--enable-ip-alias",
            "--num-nodes", "1",
            "--enable-autoscaling", "--min-nodes", "0", "--max-nodes", "10",
        ]
        if cfg.kubernetes_version:
            cmd += ["--cluster-version", cfg.kubernetes_version]
        run(cmd)
        h = ClusterHandle(name=cfg.cluster_name, region=cfg.region,
                          provider=self.name, kubeconfig=kc, created=True)
        self.get_credentials(h)
        return h

    def get_credentials(self, h: ClusterHandle) -> Path:
        env = os.environ.copy()
        env["KUBECONFIG"] = str(h.kubeconfig)
        run([
            "gcloud", "container", "clusters", "get-credentials", h.name,
            "--region", h.region,
        ], env=env)
        return h.kubeconfig

    def delete(self, h: ClusterHandle) -> None:
        if not h.created:
            return
        run([
            "gcloud", "container", "clusters", "delete", h.name,
            "--region", h.region, "--quiet",
        ], check=False)

    def node_autoprovision_hint(self) -> dict:
        return {"nodeSelector": {}, "tolerations": []}

    def cni_probe(self) -> CNIProbe:
        return get_probe("cilium_dpv2")
