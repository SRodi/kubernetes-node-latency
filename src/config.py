"""Configuration loading + dataclasses."""
from __future__ import annotations

import dataclasses as dc
from pathlib import Path
from typing import Any

import yaml


@dc.dataclass
class TriggerPodCfg:
    namespace: str = "default"
    image: str = "registry.k8s.io/pause:3.9"
    cpu: str = "1500m"
    memory: str = "2Gi"


@dc.dataclass
class CNICfg:
    probe: str | None = None
    ready_regex: str | None = None
    log_tail_lines: int = 5000


@dc.dataclass
class AKSNodePoolCfg:
    name: str = "latencypool"
    vm_size: str = "Standard_D4s_v5"
    min_count: int = 0
    max_count: int = 50
    node_count: int = 0


@dc.dataclass
class AKSSystemPoolCfg:
    name: str = "systempool"
    vm_size: str = "Standard_D4s_v5"
    node_count: int = 1


@dc.dataclass
class AKSByocniCfg:
    cilium_chart_version: str = "1.19.3"
    cilium_repo_url: str = "https://helm.cilium.io/"
    cilium_values: dict = dc.field(default_factory=lambda: {
        "kubeProxyReplacement": "true",
        "operator.replicas": "1",
    })
    install_timeout_s: int = 600


@dc.dataclass
class AKSCfg:
    resource_group: str = "node-latency-rg"
    location: str | None = None  # falls back to top-level region
    kubernetes_version: str | None = None
    node_provisioning: str = "cluster_autoscaler"  # cluster_autoscaler|nap|manual
    system_node_pool: AKSSystemPoolCfg = dc.field(default_factory=AKSSystemPoolCfg)
    user_node_pool: AKSNodePoolCfg = dc.field(default_factory=AKSNodePoolCfg)
    byocni: AKSByocniCfg = dc.field(default_factory=AKSByocniCfg)
    keep_resource_group: bool = True


@dc.dataclass
class GKEStandardCfg:
    machine_type: str = "e2-standard-4"
    num_nodes: int = 1
    min_nodes: int = 0
    max_nodes: int = 10


@dc.dataclass
class OutputCfg:
    base_dir: str = "results"
    show_plots: bool = False


@dc.dataclass
class Config:
    provider: str = "gke_autopilot"
    region: str = "europe-west1"
    cluster_name: str = "node-latency-test"
    release_channel: str = "regular"
    kubernetes_version: str | None = None
    iterations: int = 10
    per_iteration_timeout_s: int = 900
    node_settle_seconds: int = 30
    # Per-run isolation (set by CLI; enables parallel runs from different terminals).
    kubeconfig_path: Path | None = None
    cluster_name_suffix: str | None = None
    trigger_pod: TriggerPodCfg = dc.field(default_factory=TriggerPodCfg)
    cni: CNICfg = dc.field(default_factory=CNICfg)
    aks: AKSCfg = dc.field(default_factory=AKSCfg)
    gke_standard: GKEStandardCfg = dc.field(default_factory=GKEStandardCfg)
    output: OutputCfg = dc.field(default_factory=OutputCfg)

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        tp = TriggerPodCfg(**(data.pop("trigger_pod", {}) or {}))
        cni = CNICfg(**(data.pop("cni", {}) or {}))
        out = OutputCfg(**(data.pop("output", {}) or {}))
        aks_raw = data.pop("aks", {}) or {}
        sys_pool = AKSSystemPoolCfg(**(aks_raw.pop("system_node_pool", {}) or {}))
        usr_pool = AKSNodePoolCfg(**(aks_raw.pop("user_node_pool", {}) or {}))
        byocni = AKSByocniCfg(**(aks_raw.pop("byocni", {}) or {}))
        aks = AKSCfg(system_node_pool=sys_pool, user_node_pool=usr_pool,
                     byocni=byocni, **aks_raw)
        gke_std = GKEStandardCfg(**(data.pop("gke_standard", {}) or {}))
        return cls(trigger_pod=tp, cni=cni, aks=aks, gke_standard=gke_std,
                   output=out, **data)

    def merge_cli(self, **overrides: Any) -> "Config":
        for k, v in overrides.items():
            if v is not None and hasattr(self, k):
                setattr(self, k, v)
        return self
