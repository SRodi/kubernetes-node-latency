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
    trigger_pod: TriggerPodCfg = dc.field(default_factory=TriggerPodCfg)
    cni: CNICfg = dc.field(default_factory=CNICfg)
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
        return cls(trigger_pod=tp, cni=cni, output=out, **data)

    def merge_cli(self, **overrides: Any) -> "Config":
        for k, v in overrides.items():
            if v is not None and hasattr(self, k):
                setattr(self, k, v)
        return self
