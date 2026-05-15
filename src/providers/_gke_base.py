"""Shared base class for the GKE providers.

Subclasses implement ``_gcloud_create_args(cfg)`` to build the
provider-specific ``gcloud container clusters create[-auto]`` argv and
``_describe_extra()`` to surface provider-specific facts in
``run_metadata.json``. Everything else (kubeconfig path, ``get-credentials``,
delete-with-retry, autoprovision hint, default ``cilium_dpv2`` probe) is
shared verbatim.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..cni import get as get_probe
from ..cni.base import CNIProbe
from ._cli import gke_wait_for_inflight_ops, run, run_with_retry
from .base import ClusterHandle, ClusterProvider


class GKEProviderBase(ClusterProvider):
    """Common GKE create/credentials/delete plumbing."""

    name = "gke_base"
    cni_probe_name = "cilium_dpv2"

    def __init__(self, cfg):
        self.cfg = cfg

    # -- subclass hooks --
    def _gcloud_create_args(self, cfg) -> list[str]:
        raise NotImplementedError

    def _describe_extra(self) -> dict:
        return {}

    def _post_create(self, handle: ClusterHandle) -> None:
        return None

    # -- ClusterProvider --
    def _kubeconfig_path(self, cluster_name: str) -> Path:
        if getattr(self.cfg, "kubeconfig_path", None):
            return Path(self.cfg.kubeconfig_path)
        return Path.cwd() / f".kubeconfig-{self.name}-{cluster_name}"

    def create(self, cfg) -> ClusterHandle:
        kc = self._kubeconfig_path(cfg.cluster_name)
        run(self._gcloud_create_args(cfg))
        h = ClusterHandle(name=cfg.cluster_name, region=cfg.region,
                          provider=self.name, kubeconfig=kc, created=True)
        self.get_credentials(h)
        self._post_create(h)
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
        # Control-plane housekeeping ops (autorepair, scale-down) can return
        # a 400 "incompatible operation" right after the last iteration on
        # Autopilot in particular. Block on in-flight ops, then retry with
        # backoff as a safety net.
        gke_wait_for_inflight_ops(h.name, h.region)
        run_with_retry(
            ["gcloud", "container", "clusters", "delete", h.name,
             "--region", h.region, "--quiet"],
            retry_on=("incompatible operation", "FAILED_PRECONDITION",
                      "currently has operation"),
        )

    def node_autoprovision_hint(self) -> dict:
        return {"nodeSelector": {}, "tolerations": []}

    def cni_probe(self) -> CNIProbe:
        return get_probe(self.cni_probe_name)

    def describe(self, h: ClusterHandle) -> dict:
        return {
            "release_channel": self.cfg.release_channel,
            "dataplane_v2": True,
            **self._describe_extra(),
        }
