"""Shared base class for the two AKS providers."""
from __future__ import annotations

import os
from pathlib import Path

from ..cni import get as get_probe
from ..cni.base import CNIProbe
from . import _az
from .base import ClusterHandle, ClusterProvider


CLUSTER_AUTOSCALER = "cluster_autoscaler"
NAP = "nap"
MANUAL = "manual"
TRIGGER_MODES = {CLUSTER_AUTOSCALER, NAP, MANUAL}


class AKSProviderBase(ClusterProvider):
    """Common AKS create/credentials/delete plumbing.

    Subclasses implement `_az_create_cluster_args(cfg)` to inject the
    network-plugin / dataplane flags and any post-create CNI install
    (e.g. helm install for BYOCNI).
    """
    name = "aks_base"

    def __init__(self, cfg):
        self.cfg = cfg
        mode = cfg.aks.node_provisioning
        if mode not in TRIGGER_MODES:
            raise ValueError(f"unknown node_provisioning mode '{mode}'. "
                             f"valid: {sorted(TRIGGER_MODES)}")
        self._mode = mode

    # -- subclass hooks --
    def _az_create_cluster_args(self, cfg) -> list[str]:
        raise NotImplementedError

    def _post_create(self, handle: ClusterHandle) -> None:
        return None

    # -- ClusterProvider --
    def _kubeconfig_path(self, cluster_name: str) -> Path:
        if getattr(self.cfg, "kubeconfig_path", None):
            return Path(self.cfg.kubeconfig_path)
        return Path.cwd() / f".kubeconfig-{self.name}-{cluster_name}"

    def create(self, cfg) -> ClusterHandle:
        location = cfg.aks.location or cfg.region
        rg = cfg.aks.resource_group
        kc = self._kubeconfig_path(cfg.cluster_name)

        _az.ensure_resource_group(rg, location)

        sys_pool = cfg.aks.system_node_pool
        create_args = [
            "aks", "create", "-g", rg, "-n", cfg.cluster_name,
            "--location", location,
            "--node-count", str(sys_pool.node_count),
            "--node-vm-size", sys_pool.vm_size,
            "--nodepool-name", sys_pool.name,
            "--enable-managed-identity",
            "--generate-ssh-keys",
        ]
        if cfg.aks.kubernetes_version:
            create_args += ["--kubernetes-version", cfg.aks.kubernetes_version]
        if cfg.aks.long_term_support:
            # LTS-only versions require Premium tier + the LTS support plan.
            create_args += ["--tier", "premium",
                            "--k8s-support-plan", "AKSLongTermSupport"]
        create_args += self._az_create_cluster_args(cfg)

        if self._mode == NAP:
            # NAP (preview): enable node-provisioning auto on the cluster
            create_args += ["--node-provisioning-mode", "Auto"]

        _az.az(create_args)

        # User node pool (for trigger pods) - only for cluster_autoscaler/manual
        if self._mode != NAP:
            up = cfg.aks.user_node_pool
            np_args = [
                "aks", "nodepool", "add",
                "-g", rg, "--cluster-name", cfg.cluster_name,
                "-n", up.name,
                "--node-vm-size", up.vm_size,
                "--node-count", str(up.node_count),
                "--mode", "User",
            ]
            if self._mode == CLUSTER_AUTOSCALER:
                np_args += [
                    "--enable-cluster-autoscaler",
                    "--min-count", str(up.min_count),
                    "--max-count", str(up.max_count),
                ]
            _az.az(np_args)

        handle = ClusterHandle(name=cfg.cluster_name, region=location,
                                provider=self.name, kubeconfig=kc, created=True,
                                extra={"resource_group": rg, "mode": self._mode})
        self.get_credentials(handle)
        self._post_create(handle)
        return handle

    def get_credentials(self, h: ClusterHandle) -> Path:
        rg = h.extra.get("resource_group", self.cfg.aks.resource_group)
        return _az.aks_get_credentials(rg, h.name, h.kubeconfig)

    def delete(self, h: ClusterHandle) -> None:
        if not h.created:
            return
        rg = h.extra.get("resource_group", self.cfg.aks.resource_group)
        _az.aks_delete(rg, h.name)
        if not self.cfg.aks.keep_resource_group:
            _az.az(["group", "delete", "-n", rg, "--yes", "--no-wait"], check=False)

    def node_autoprovision_hint(self) -> dict:
        if self._mode == NAP:
            return {"nodeSelector": {}, "tolerations": []}
        # Land trigger pods on the user pool so the system pool isn't disturbed.
        return {"nodeSelector": {"agentpool": self.cfg.aks.user_node_pool.name},
                "tolerations": []}

    def cni_probe(self) -> CNIProbe:
        return get_probe(self.cfg.cni.probe or "cilium_generic")

    def describe(self, h: ClusterHandle) -> dict:
        return {
            "node_provisioning": self._mode,
            "system_pool_vm": self.cfg.aks.system_node_pool.vm_size,
            "user_pool_vm": self.cfg.aks.user_node_pool.vm_size,
            "resource_group": self.cfg.aks.resource_group,
            **self._network_describe(),
        }

    def _network_describe(self) -> dict:
        # Subclasses override with their network plugin/dataplane facts.
        return {}

    # -- per-iteration hooks --
    def pre_iteration(self, h: ClusterHandle, iteration: int) -> None:
        if self._mode == MANUAL:
            up = self.cfg.aks.user_node_pool
            rg = h.extra.get("resource_group", self.cfg.aks.resource_group)
            _az.aks_nodepool_scale(rg, h.name, up.name, count=up.node_count + 1)

    def post_iteration(self, h: ClusterHandle, iteration: int) -> None:
        if self._mode == MANUAL:
            up = self.cfg.aks.user_node_pool
            rg = h.extra.get("resource_group", self.cfg.aks.resource_group)
            _az.aks_nodepool_scale(rg, h.name, up.name, count=up.node_count)
