"""GKE Standard with Dataplane V2."""
from __future__ import annotations

from ._gke_base import GKEProviderBase


class GKEStandardDPv2Provider(GKEProviderBase):
    name = "gke_standard_dpv2"

    def _gcloud_create_args(self, cfg) -> list[str]:
        gs = cfg.gke_standard
        cmd = [
            "gcloud", "container", "clusters", "create", cfg.cluster_name,
            "--region", cfg.region,
            "--release-channel", cfg.release_channel,
            "--enable-dataplane-v2",
            "--enable-ip-alias",
            "--machine-type", gs.machine_type,
            "--num-nodes", str(gs.num_nodes),
            "--enable-autoscaling",
            "--min-nodes", str(gs.min_nodes),
            "--max-nodes", str(gs.max_nodes),
        ]
        if cfg.kubernetes_version:
            cmd += ["--cluster-version", cfg.kubernetes_version]
        if getattr(cfg.cni, "deep", False):
            cmd += ["--enable-dataplane-v2-flow-observability"]
        return cmd

    def _describe_extra(self) -> dict:
        gs = self.cfg.gke_standard
        return {
            "flavor": "standard",
            "machine_type": gs.machine_type,
            "autoscaling": f"min={gs.min_nodes},max={gs.max_nodes}",
        }
