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
    # Sized to force AKS NAP (Karpenter) to provision an 8-vCPU / 32-GiB
    # SKU (e.g. Standard_D8s_v5), matching GKE Autopilot's default
    # `ek-standard-8` node class. NAP picks the cheapest SKU that fits the
    # pod request plus system overhead; 6000m / 16Gi rules out D2/D4 SKUs.
    # GKE Autopilot accommodates this just as easily (single pod, no impact
    # on its node-class selection since it already lands on ek-standard-8).
    cpu: str = "6000m"
    memory: str = "16Gi"


@dc.dataclass
class CNICfg:
    probe: str | None = None
    ready_regex: str | None = None
    log_tail_lines: int = 5000
    # Tier-1 deep-Cilium capture: scrape the agent's Prometheus metrics
    # endpoint after T3 fires, via a one-shot scraper Pod pinned to the new
    # node (works on Autopilot, AKS distroless cilium, BYOCNI, etc.).
    deep: bool = False
    metrics_ports: list[int] = dc.field(
        default_factory=lambda: [9990, 9962, 6942, 9963, 9090])
    deep_scraper_image: str = "curlimages/curl:8.11.1"
    deep_scraper_namespace: str = "default"


@dc.dataclass
class AKSNodePoolCfg:
    name: str = "latencypool"
    # Standard_D8s_v5 = 8 vCPU / 32 GiB RAM — matches GKE Autopilot's default
    # `ek-standard-8` node class so cross-provider comparisons aren't skewed
    # by per-node CPU/memory headroom (image-pull, container-start, BPF
    # compilation all scale with CPU).
    vm_size: str = "Standard_D8s_v5"
    min_count: int = 0
    max_count: int = 50
    node_count: int = 0


@dc.dataclass
class AKSSystemPoolCfg:
    name: str = "systempool"
    vm_size: str = "Standard_D8s_v5"
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
    # Dedicated nodepool for trigger pods. Mirrors the AKS user-pool pattern:
    # starts at 0 nodes with autoscaling, trigger pods are pinned here via
    # nodeSelector so every iteration deterministically provisions a fresh
    # VM (no cordon-and-pray dance with cluster autoscaler).
    trigger_pool_name: str = "latencypool"
    trigger_pool_machine_type: str = "e2-standard-4"
    trigger_pool_min_nodes: int = 0
    trigger_pool_max_nodes: int = 50


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
