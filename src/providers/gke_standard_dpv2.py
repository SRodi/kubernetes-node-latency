"""GKE Standard with Dataplane V2."""
from __future__ import annotations

import logging

from ._cli import run
from ._gke_base import GKEProviderBase
from .base import ClusterHandle

log = logging.getLogger(__name__)

# Label key/value applied to the trigger nodepool and used as the trigger
# pod's nodeSelector. Mirrors the AKS `agentpool=<userpool>` pattern.
TRIGGER_POOL_LABEL_KEY = "nodepool"


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

    def _post_create(self, h: ClusterHandle) -> None:
        """Add a dedicated zero-node trigger nodepool with autoscaling.

        Every benchmark iteration places its trigger pod here via
        nodeSelector. Because the pool starts at 0 nodes, cluster autoscaler
        is *forced* to provision a fresh VM (no reliance on the cordon-based
        scale-up heuristic, which GKE CA does not honour reliably).
        """
        gs = self.cfg.gke_standard
        log.info(
            "creating trigger nodepool %r (min=%d, max=%d, machine=%s)",
            gs.trigger_pool_name, gs.trigger_pool_min_nodes,
            gs.trigger_pool_max_nodes, gs.trigger_pool_machine_type,
        )
        run([
            "gcloud", "container", "node-pools", "create", gs.trigger_pool_name,
            "--cluster", h.name,
            "--region", h.region,
            "--machine-type", gs.trigger_pool_machine_type,
            "--num-nodes", "0",
            "--enable-autoscaling",
            "--min-nodes", str(gs.trigger_pool_min_nodes),
            "--max-nodes", str(gs.trigger_pool_max_nodes),
            "--node-labels", f"{TRIGGER_POOL_LABEL_KEY}={gs.trigger_pool_name}",
        ])

    def node_autoprovision_hint(self) -> dict:
        # Pin trigger pods to the dedicated pool so cluster autoscaler must
        # scale it up from zero on every iteration.
        return {
            "nodeSelector": {
                TRIGGER_POOL_LABEL_KEY: self.cfg.gke_standard.trigger_pool_name,
            },
            "tolerations": [],
        }

    def _describe_extra(self) -> dict:
        gs = self.cfg.gke_standard
        return {
            "flavor": "standard",
            "machine_type": gs.machine_type,
            "autoscaling": f"min={gs.min_nodes},max={gs.max_nodes}",
            "trigger_pool": {
                "name": gs.trigger_pool_name,
                "machine_type": gs.trigger_pool_machine_type,
                "autoscaling": (
                    f"min={gs.trigger_pool_min_nodes},"
                    f"max={gs.trigger_pool_max_nodes}"
                ),
            },
        }
