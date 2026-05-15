"""GKE Autopilot provider."""
from __future__ import annotations

from ._gke_base import GKEProviderBase


class GKEAutopilotProvider(GKEProviderBase):
    name = "gke_autopilot"

    def _gcloud_create_args(self, cfg) -> list[str]:
        cmd = [
            "gcloud", "container", "clusters", "create-auto", cfg.cluster_name,
            "--region", cfg.region,
            "--release-channel", cfg.release_channel,
        ]
        if cfg.kubernetes_version:
            cmd += ["--cluster-version", cfg.kubernetes_version]
        if getattr(cfg.cni, "deep", False):
            # Enables Hubble flow observability on anetd; helps surface
            # additional metrics on managed clusters. The actual scrape ports
            # are auto-discovered from the agent Pod spec at run-time.
            cmd += ["--enable-dataplane-v2-flow-observability"]
        return cmd

    def _describe_extra(self) -> dict:
        return {"flavor": "autopilot"}
