"""Unit tests for the deep-Cilium tier-1 capture."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import cilium_deep
from src.records import IterationRecord


SAMPLE_METRICS = """
# HELP cilium_bootstrap_seconds Cilium bootstrap durations
# TYPE cilium_bootstrap_seconds gauge
cilium_bootstrap_seconds{scope="k8sInit"} 1.234
cilium_bootstrap_seconds{scope="bpfBase"} 4.5
cilium_bootstrap_seconds{scope="ipam"} 0.6
cilium_bootstrap_seconds{scope="total"} 12.34
# HELP cilium_endpoint_regeneration_time_stats_seconds histogram
# TYPE cilium_endpoint_regeneration_time_stats_seconds histogram
cilium_endpoint_regeneration_time_stats_seconds_sum{scope="total"} 2.5
cilium_endpoint_regeneration_time_stats_seconds_count{scope="total"} 5
cilium_endpoint_regeneration_time_stats_seconds_sum{scope="bpfLoadProg"} 1.0
cilium_endpoint_regeneration_time_stats_seconds_count{scope="bpfLoadProg"} 5
# HELP cilium_endpoint_state gauge
# TYPE cilium_endpoint_state gauge
cilium_endpoint_state{state="ready"} 7
cilium_endpoint_state{state="waiting-for-identity"} 0
cilium_identity_count 42
cilium_bpf_map_pressure{map_name="cilium_lb"} 0.05
cilium_bpf_map_pressure{map_name="cilium_ct"} 0.12
unrelated_metric{foo="bar"} 999
"""


def test_parse_metrics_extracts_headlines():
    h = cilium_deep.parse_metrics(SAMPLE_METRICS)
    assert h["bootstrap"] == {"k8sInit": 1.234, "bpfBase": 4.5, "ipam": 0.6, "total": 12.34}
    assert h["endpoint_regeneration_avg_s"]["total"] == 0.5
    assert h["endpoint_regeneration_avg_s"]["bpfLoadProg"] == 0.2
    assert h["endpoint_state"]["ready"] == 7
    assert h["identity_count"] == 42
    assert h["bpf_map_pressure_max"] == 0.12


def test_headline_to_columns_handles_missing_data():
    cols = cilium_deep.headline_to_columns(None)
    assert set(cols.keys()) == set(cilium_deep.HEADLINE_COLUMNS)
    assert all(v is None for v in cols.values())


def test_headline_to_columns_projects_known_fields():
    headline = {
        "cilium_version": "1.18.7-gke1.35",
        "bootstrap": {
            "total": 12.34, "k8sInit": 1.0, "restoreState": 0.5,
            "bpfBase": 4.5, "ipam": 0.6, "proxyInit": 1.1,
        },
        "metrics": {
            "endpoint_regeneration_avg_s": {"total": 0.5},
            "identity_count": 42,
        },
    }
    cols = cilium_deep.headline_to_columns(headline)
    assert cols["cilium_bootstrap_total_s"] == 12.34
    assert cols["cilium_bootstrap_k8s_init_s"] == 1.0
    assert cols["cilium_bootstrap_restore_s"] == 0.5
    assert cols["cilium_bootstrap_bpf_base_s"] == 4.5
    assert cols["cilium_bootstrap_ipam_s"] == 0.6
    assert cols["cilium_bootstrap_proxy_s"] == 1.1
    assert cols["cilium_endpoint_regen_avg_s"] == 0.5
    assert cols["cilium_identity_count"] == 42
    assert cols["cilium_version"] == "1.18.7-gke1.35"


def test_iteration_record_to_row_includes_deep_columns_when_set():
    rec = IterationRecord(iteration=1, run_id="r", provider="p", region="r")
    rec.deep_cilium = {"bootstrap": {"total": 9.0, "ipam": 1.5},
                       "metrics": {"endpoint_regeneration_avg_s": {"total": 0.3},
                                    "identity_count": 11}}
    row = rec.to_row()
    for col in cilium_deep.HEADLINE_COLUMNS:
        assert col in row
    assert row["cilium_bootstrap_total_s"] == 9.0
    assert row["cilium_bootstrap_ipam_s"] == 1.5
    assert row["cilium_endpoint_regen_avg_s"] == 0.3
    assert row["cilium_identity_count"] == 11


def test_iteration_record_to_row_deep_columns_null_when_unset():
    rec = IterationRecord(iteration=1, run_id="r", provider="p", region="r")
    row = rec.to_row()
    for col in cilium_deep.HEADLINE_COLUMNS:
        assert row[col] is None


def test_collect_writes_artifacts_and_returns_headline(tmp_path: Path):
    core = MagicMock()
    pod = MagicMock()
    pod.metadata.namespace = "kube-system"
    pod.metadata.name = "cilium-xyz"
    probe = MagicMock()
    probe.container_name = "cilium-agent"

    fake_status = {
        "cilium": {"version": "1.18.6"},
        "ipam": {"mode": "cluster-pool", "status": "ok"},
        "kube-proxy-replacement": {"mode": "Strict"},
        "bootstrap": {"total": 10.5, "k8sInit": 1.0, "ipam": 2.0,
                       "bpfBase": 4.0, "proxyInit": 0.5},
    }

    with patch.object(cilium_deep, "fetch_status", return_value=fake_status), \
         patch.object(cilium_deep, "fetch_metrics", return_value=SAMPLE_METRICS):
        headline = cilium_deep.collect(
            core, agent_pod=pod, probe=probe, iter_dir=tmp_path,
            metrics_ports=[9962])

    assert (tmp_path / "cilium_status.json").exists()
    assert (tmp_path / "cilium_metrics.txt").exists()
    assert (tmp_path / "cilium_deep_headline.json").exists()
    assert headline["cilium_version"] == "1.18.6"
    assert headline["ipam_mode"] == "cluster-pool"
    assert headline["bootstrap"]["total"] == 10.5
    assert headline["metrics"]["identity_count"] == 42

    # Validate persisted JSON is real, parseable.
    on_disk = json.loads((tmp_path / "cilium_status.json").read_text())
    assert on_disk["cilium"]["version"] == "1.18.6"
