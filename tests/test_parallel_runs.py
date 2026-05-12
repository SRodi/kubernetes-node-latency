"""Tests for per-run kubeconfig + cluster-name isolation enabling parallel runs."""
from __future__ import annotations

from pathlib import Path

from src.config import Config
from src.providers.gke_autopilot import GKEAutopilotProvider
from src.providers.aks_overlay_cilium import AKSOverlayCiliumProvider


def _aks_cfg() -> Config:
    return Config.from_dict({
        "provider": "aks_overlay_cilium", "region": "westeurope",
        "cluster_name": "node-latency-test",
        "aks": {"resource_group": "rg-x"},
    })


def test_kubeconfig_path_honours_cfg_override(tmp_path: Path):
    cfg = Config()
    cfg.kubeconfig_path = tmp_path / "kc"
    p = GKEAutopilotProvider(cfg)
    assert p._kubeconfig_path("any") == tmp_path / "kc"


def test_kubeconfig_path_default_when_unset():
    cfg = Config()
    assert cfg.kubeconfig_path is None
    p = GKEAutopilotProvider(cfg)
    assert "node-latency-test" in str(p._kubeconfig_path("node-latency-test"))


def test_two_parallel_runs_get_distinct_kubeconfig_and_cluster_name(tmp_path: Path):
    # Simulate what cli._cmd_run does for two concurrent runs.
    def derive(cfg: Config, run_id: str, base: Path) -> tuple[Path, str]:
        run_dir = base / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg.kubeconfig_path = (run_dir / "kubeconfig").resolve()
        cfg.cluster_name_suffix = run_id[-6:]
        cfg.cluster_name = f"{cfg.cluster_name}-{cfg.cluster_name_suffix}"
        return cfg.kubeconfig_path, cfg.cluster_name

    a_kc, a_name = derive(_aks_cfg(), "20260512-100000", tmp_path)
    b_kc, b_name = derive(_aks_cfg(), "20260512-100001", tmp_path)
    assert a_kc != b_kc
    assert a_name != b_name
    assert a_name.endswith("100000") and b_name.endswith("100001")


def test_aks_provider_uses_cfg_kubeconfig_path(tmp_path: Path):
    cfg = _aks_cfg()
    cfg.kubeconfig_path = tmp_path / "kc"
    p = AKSOverlayCiliumProvider(cfg)
    assert p._kubeconfig_path(cfg.cluster_name) == tmp_path / "kc"
