"""Tests for run_metadata.json generation."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from src.config import Config
from src.metadata import (append_summary_section, finalize_metadata,
                            gather_metadata, write_metadata)
from src.providers.base import ClusterHandle


def _fake_node(name: str, version: str = "v1.30.0") -> MagicMock:
    n = MagicMock()
    n.metadata.name = name
    n.metadata.creation_timestamp = None
    n.metadata.labels = {
        "node.kubernetes.io/instance-type": "e2-standard-4",
        "topology.kubernetes.io/region": "europe-west1",
        "topology.kubernetes.io/zone": "europe-west1-b",
        "irrelevant": "ignored",
    }
    n.status.node_info.kubelet_version = version
    n.status.node_info.container_runtime_version = "containerd://1.7.0"
    n.status.node_info.os_image = "Container-Optimized OS"
    n.status.node_info.kernel_version = "6.1.0"
    n.status.node_info.architecture = "amd64"
    return n


def _fake_pod_with_container(image: str, container: str) -> MagicMock:
    p = MagicMock()
    c = MagicMock()
    c.name = container
    c.image = image
    p.spec.containers = [c]
    return p


def _stub_provider(name: str = "test_prov", describe: dict | None = None):
    prov = MagicMock()
    prov.name = name
    probe = MagicMock()
    probe.namespace = "kube-system"
    probe.label_selector = "k8s-app=cilium"
    probe.container_name = "cilium-agent"
    prov.cni_probe.return_value = probe
    if describe is not None:
        prov.describe.return_value = describe
    else:
        del prov.describe  # simulate provider without describe()
    return prov, probe


def test_gather_metadata_includes_config_cluster_and_describe(tmp_path: Path):
    cfg = Config()
    handle = ClusterHandle(name="c1", region="europe-west1",
                           provider="test_prov", kubeconfig=tmp_path / "kc",
                           created=True, extra={"resource_group": "rg-x"})
    prov, probe = _stub_provider("test_prov",
                                  describe={"flavor": "autopilot", "dataplane_v2": True})
    core = MagicMock()
    core.list_node.return_value.items = [_fake_node("n1"), _fake_node("n2")]
    core.list_namespaced_pod.return_value.items = [
        _fake_pod_with_container("cilium/cilium:v1.16.5", "cilium-agent")
    ]

    meta = gather_metadata(cfg=cfg, handle=handle, provider=prov, core=core,
                            run_id="20260512-120000",
                            cli_argv=["run", "--provider", "test_prov"])

    assert meta["run_id"] == "20260512-120000"
    assert meta["cluster"]["provider"] == "test_prov"
    assert meta["cluster"]["region"] == "europe-west1"
    assert meta["cluster"]["node_count_at_start"] == 2
    assert meta["cluster"]["kubernetes_version"] == "v1.30.0"
    assert meta["cluster"]["cni"]["image"] == "cilium/cilium:v1.16.5"
    assert "irrelevant" not in meta["cluster"]["nodes"][0]["labels"]
    assert meta["cluster"]["nodes"][0]["labels"]["node.kubernetes.io/instance-type"] == "e2-standard-4"
    assert meta["provider_describe"]["flavor"] == "autopilot"
    assert meta["status"] == "running"
    assert meta["config"]["provider"] == "gke_autopilot"  # default


def test_provider_without_describe_falls_back_to_empty(tmp_path: Path):
    cfg = Config()
    handle = ClusterHandle(name="c", region="r", provider="p",
                           kubeconfig=tmp_path / "kc", created=False)
    prov, _ = _stub_provider(describe=None)
    core = MagicMock()
    core.list_node.return_value.items = []
    core.list_namespaced_pod.return_value.items = []
    meta = gather_metadata(cfg=cfg, handle=handle, provider=prov, core=core,
                            run_id="x", cli_argv=[])
    assert meta["provider_describe"] == {}


def test_write_finalize_and_summary_section(tmp_path: Path):
    meta = {
        "run_id": "x",
        "schema_version": 1,
        "start_time": "2026-05-12T12:00:00+00:00",
        "end_time": None,
        "duration_s": None,
        "status": "running",
        "cli_argv": [],
        "harness_git_commit": None,
        "tooling_versions": {},
        "config": {},
        "cluster": {
            "provider": "gke_autopilot", "region": "europe-west1", "name": "c1",
            "created_by_harness": True, "kubeconfig": "kc", "extra": {},
            "node_count_at_start": 3, "kubernetes_version": "v1.30.0",
            "nodes": [{"labels": {"node.kubernetes.io/instance-type": "e2-medium"}}],
            "cni": {"image": "anetd:v1.16", "container": "cilium-agent"},
        },
        "provider_describe": {"flavor": "autopilot"},
    }
    write_metadata(tmp_path, meta)
    final = finalize_metadata(tmp_path, status="success")
    assert final is not None
    assert final["status"] == "success"
    assert final["end_time"] is not None
    assert final["duration_s"] is not None
    on_disk = json.loads((tmp_path / "run_metadata.json").read_text())
    assert on_disk["status"] == "success"

    summary = tmp_path / "summary.md"
    summary.write_text("# title\n")
    append_summary_section(summary, final)
    body = summary.read_text()
    assert "## Cluster" in body
    assert "gke_autopilot" in body
    assert "v1.30.0" in body
    assert "e2-medium" in body
    assert "anetd:v1.16" in body
    assert "flavor=autopilot" in body
