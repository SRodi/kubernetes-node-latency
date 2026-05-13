"""Unit tests for deep-Cilium capture (universal scraper-Pod strategy)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes import client

from src import cilium_deep
from src.records import IterationRecord


SAMPLE_METRICS = """
# HELP cilium_bootstrap_seconds Cilium bootstrap durations
# TYPE cilium_bootstrap_seconds gauge
cilium_bootstrap_seconds{scope="k8sInit"} 1.234
cilium_bootstrap_seconds{scope="bpfBase"} 4.5
cilium_bootstrap_seconds{scope="ipam"} 0.6
cilium_bootstrap_seconds{scope="total"} 12.34
cilium_endpoint_regeneration_time_stats_seconds_sum{scope="total"} 2.5
cilium_endpoint_regeneration_time_stats_seconds_count{scope="total"} 5
cilium_endpoint_state{state="ready"} 7
cilium_identity_count 42
cilium_version_info{version="1.18.6"} 1
"""


# ---------- parser ---------------------------------------------------------

def test_parse_metrics_extracts_headlines():
    p = cilium_deep.parse_metrics(SAMPLE_METRICS)
    assert p["bootstrap"]["total"] == 12.34
    assert p["bootstrap"]["k8sInit"] == 1.234
    assert p["endpoint_regeneration_avg_s"]["total"] == 0.5
    assert p["identity_count"] == 42
    assert p["version"] == "1.18.6"


def test_parse_metrics_empty_returns_defaults():
    p = cilium_deep.parse_metrics("")
    assert p["bootstrap"] == {}
    assert p["identity_count"] is None
    assert p["version"] is None


# ---------- headline projection -------------------------------------------

def test_headline_to_columns_with_data():
    headline = {
        "bootstrap": {"total": 12.3, "k8sInit": 1.2, "bpfBase": 4.5, "ipam": 0.6},
        "metrics": {
            "endpoint_regeneration_avg_s": {"total": 0.5},
            "identity_count": 42,
        },
        "cilium_version": "1.18.6",
    }
    cols = cilium_deep.headline_to_columns(headline)
    assert cols["cilium_bootstrap_total_s"] == 12.3
    assert cols["cilium_bootstrap_k8s_init_s"] == 1.2
    assert cols["cilium_endpoint_regen_avg_s"] == 0.5
    assert cols["cilium_identity_count"] == 42
    assert cols["cilium_version"] == "1.18.6"


def test_headline_to_columns_none_returns_all_nones():
    cols = cilium_deep.headline_to_columns(None)
    assert all(v is None for v in cols.values())
    assert "cilium_bootstrap_total_s" in cols


# ---------- record integration --------------------------------------------

def test_iteration_record_to_row_merges_deep_columns():
    rec = IterationRecord(iteration=1, run_id="r", provider="p", region="x",
                           node_name="n")
    rec.deep_cilium = {
        "bootstrap": {"total": 9.9},
        "metrics": {"identity_count": 5, "endpoint_regeneration_avg_s": {}},
        "cilium_version": "1.18.6",
    }
    row = rec.to_row()
    assert row["cilium_bootstrap_total_s"] == 9.9
    assert row["cilium_identity_count"] == 5
    assert row["cilium_version"] == "1.18.6"


# ---------- scraper Pod ---------------------------------------------------

def _agent(pod_ip: str | None = "10.0.0.5", image: str = "anetd:v1",
            container_ports: list[int] | None = None):
    ports = []
    for p in (container_ports or []):
        ports.append(SimpleNamespace(container_port=p, protocol="TCP"))
    return SimpleNamespace(
        metadata=SimpleNamespace(name="anetd-x", namespace="kube-system"),
        status=SimpleNamespace(pod_ip=pod_ip),
        spec=SimpleNamespace(containers=[SimpleNamespace(name="cilium-agent",
                                                          image=image,
                                                          ports=ports)]),
    )


def test_fetch_metrics_probes_declared_ports_first(monkeypatch):
    core = MagicMock()
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Succeeded"))
    core.read_namespaced_pod_log.return_value = (
        "=== METRICS port=9990 ===\n" + SAMPLE_METRICS)
    monkeypatch.setattr(cilium_deep.time, "sleep", lambda *_: None)

    out = cilium_deep.fetch_metrics(
        core, agent_pod=_agent(container_ports=[9990, 4244]),
        node_name="n", ports=[9962, 9090])
    assert "cilium_bootstrap_seconds" in out
    body = core.create_namespaced_pod.call_args[0][1]
    script = body["spec"]["containers"][0]["command"][2]
    # Declared ports come before configured fallbacks; no duplicates.
    assert 'PORTS="9990 4244 9962 9090"' in script


def test_fetch_metrics_happy_path(monkeypatch):
    core = MagicMock()
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Succeeded"))
    core.read_namespaced_pod_log.return_value = (
        "=== probe port=9962 ===\nHTTP=200 BYTES=99\n"
        "=== METRICS port=9962 ===\n" + SAMPLE_METRICS
    )
    monkeypatch.setattr(cilium_deep.time, "sleep", lambda *_: None)

    out = cilium_deep.fetch_metrics(core, agent_pod=_agent(),
                                     node_name="node-a", ports=[9962])
    assert "cilium_bootstrap_seconds" in out
    core.create_namespaced_pod.assert_called_once()
    ns, body = core.create_namespaced_pod.call_args[0]
    assert ns == "default"
    assert body["spec"]["nodeName"] == "node-a"
    script = body["spec"]["containers"][0]["command"][2]
    assert 'AGENT_IP="10.0.0.5"' in script
    assert 'PORTS="9962"' in script
    core.delete_namespaced_pod.assert_called_once()


def test_fetch_metrics_no_pod_ip_retries_then_returns_none(monkeypatch):
    core = MagicMock()
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(pod_ip=None))
    monkeypatch.setattr(cilium_deep.time, "sleep", lambda *_: None)
    out = cilium_deep.fetch_metrics(core, agent_pod=_agent(pod_ip=None),
                                     node_name="n", ports=[9962])
    assert out is None
    core.create_namespaced_pod.assert_not_called()


def test_fetch_metrics_no_pod_ip_recovers_via_refetch(monkeypatch):
    core = MagicMock()
    refreshed = SimpleNamespace(status=SimpleNamespace(pod_ip="10.0.0.9"))
    succeeded = SimpleNamespace(status=SimpleNamespace(phase="Succeeded"))
    # First call: refresh PodIP. Subsequent calls: scraper-pod phase polls.
    core.read_namespaced_pod.side_effect = [refreshed, succeeded]
    core.read_namespaced_pod_log.return_value = (
        "=== METRICS port=9962 ===\n" + SAMPLE_METRICS)
    monkeypatch.setattr(cilium_deep.time, "sleep", lambda *_: None)

    out = cilium_deep.fetch_metrics(core, agent_pod=_agent(pod_ip=None),
                                     node_name="n", ports=[9962])
    assert "cilium_bootstrap_seconds" in out
    body = core.create_namespaced_pod.call_args[0][1]
    assert 'AGENT_IP="10.0.0.9"' in body["spec"]["containers"][0]["command"][2]


def test_fetch_metrics_pod_failed_with_diag_returns_diag(monkeypatch):
    core = MagicMock()
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Succeeded"))
    core.read_namespaced_pod_log.return_value = (
        "=== probe port=9962 ===\nHTTP=000 BYTES=0\n"
        "=== probe port=9090 ===\nHTTP=000 BYTES=0\n"
    )
    monkeypatch.setattr(cilium_deep.time, "sleep", lambda *_: None)

    out = cilium_deep.fetch_metrics(core, agent_pod=_agent(),
                                     node_name="n", ports=[9962, 9090])
    # Diag log returned to caller, but no metrics inside.
    assert out is not None and "cilium_" not in out
    core.delete_namespaced_pod.assert_called_once()


def test_fetch_metrics_create_failure_returns_none():
    core = MagicMock()
    core.create_namespaced_pod.side_effect = client.ApiException(
        status=403, reason="Forbidden")
    out = cilium_deep.fetch_metrics(core, agent_pod=_agent(),
                                     node_name="n", ports=[9962])
    assert out is None
    core.delete_namespaced_pod.assert_not_called()


def test_fetch_metrics_cleanup_runs_even_on_log_failure(monkeypatch):
    core = MagicMock()
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Succeeded"))
    core.read_namespaced_pod_log.side_effect = client.ApiException(
        status=500, reason="boom")
    monkeypatch.setattr(cilium_deep.time, "sleep", lambda *_: None)

    out = cilium_deep.fetch_metrics(core, agent_pod=_agent(),
                                     node_name="n", ports=[9962])
    assert out is None
    core.delete_namespaced_pod.assert_called_once()


# ---------- collect() end-to-end ------------------------------------------

def test_collect_writes_files_and_returns_headline(tmp_path, monkeypatch):
    core = MagicMock()
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Succeeded"))
    core.read_namespaced_pod_log.return_value = (
        "=== probe port=9962 ===\nHTTP=200 BYTES=99\n"
        "=== METRICS port=9962 ===\n" + SAMPLE_METRICS
    )
    monkeypatch.setattr(cilium_deep.time, "sleep", lambda *_: None)

    probe = SimpleNamespace(container_name="cilium-agent")
    iter_dir = tmp_path / "iter-001"
    out = cilium_deep.collect(
        core, agent_pod=_agent(image="anetd:v1.18.6"), probe=probe,
        node_name="node-a", iter_dir=iter_dir, metrics_ports=[9962])

    assert (iter_dir / "cilium_metrics.txt").exists()
    assert (iter_dir / "scraper_probe.log").exists()
    headline = json.loads((iter_dir / "cilium_deep_headline.json").read_text())
    assert headline["bootstrap"]["total"] == 12.34
    assert out["cilium_version"] in ("1.18.6", "anetd:v1.18.6")


def test_collect_skips_iter_dir_when_no_metrics(tmp_path, monkeypatch):
    core = MagicMock()
    # No PodIP and refresh also returns no PodIP → fetch_metrics returns None
    # → collect must not create iter_dir.
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(pod_ip=None))
    monkeypatch.setattr(cilium_deep.time, "sleep", lambda *_: None)
    iter_dir = tmp_path / "iter-002"
    probe = SimpleNamespace(container_name="cilium-agent")
    out = cilium_deep.collect(
        core, agent_pod=_agent(pod_ip=None), probe=probe,
        node_name="n", iter_dir=iter_dir, metrics_ports=[9962])
    assert out == {}
    assert not iter_dir.exists()


def test_collect_persists_diag_when_no_port_works(tmp_path, monkeypatch):
    core = MagicMock()
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Succeeded"))
    core.read_namespaced_pod_log.return_value = (
        "=== probe port=9962 ===\nHTTP=000 BYTES=0\n"
        "=== probe port=9090 ===\nHTTP=000 BYTES=0\n"
    )
    monkeypatch.setattr(cilium_deep.time, "sleep", lambda *_: None)
    iter_dir = tmp_path / "iter-003"
    probe = SimpleNamespace(container_name="cilium-agent")
    out = cilium_deep.collect(
        core, agent_pod=_agent(), probe=probe,
        node_name="n", iter_dir=iter_dir, metrics_ports=[9962, 9090])
    assert out == {}
    assert (iter_dir / "scraper_probe.log").exists()
    assert "HTTP=000" in (iter_dir / "scraper_probe.log").read_text()
