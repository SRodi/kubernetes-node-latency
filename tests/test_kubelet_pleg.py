"""Unit tests for kubelet PLEG capture (apiserver node-proxy strategy)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from kubernetes import client

from src import kubelet_pleg
from src.records import IterationRecord


# A trimmed but realistic kubelet /metrics dump. Relist-duration bucket
# counts are cumulative; total (count) = 200, sum = 0.30s → avg 1.5ms.
# The vast majority (195/200) land under 0.005s, so p50/p90 ≈ 0.005s and
# p99 falls in the (0.01, 0.025] bucket.
SAMPLE_KUBELET_METRICS = """
# HELP kubelet_pleg_relist_duration_seconds [ALPHA] Duration in seconds for relisting pods in PLEG.
# TYPE kubelet_pleg_relist_duration_seconds histogram
kubelet_pleg_relist_duration_seconds_bucket{le="0.005"} 195
kubelet_pleg_relist_duration_seconds_bucket{le="0.01"} 197
kubelet_pleg_relist_duration_seconds_bucket{le="0.025"} 200
kubelet_pleg_relist_duration_seconds_bucket{le="0.05"} 200
kubelet_pleg_relist_duration_seconds_bucket{le="+Inf"} 200
kubelet_pleg_relist_duration_seconds_sum 0.30
kubelet_pleg_relist_duration_seconds_count 200
# HELP kubelet_pleg_relist_interval_seconds [ALPHA] Interval in seconds between relisting in PLEG.
# TYPE kubelet_pleg_relist_interval_seconds histogram
kubelet_pleg_relist_interval_seconds_bucket{le="0.5"} 0
kubelet_pleg_relist_interval_seconds_bucket{le="1"} 190
kubelet_pleg_relist_interval_seconds_bucket{le="2"} 199
kubelet_pleg_relist_interval_seconds_bucket{le="+Inf"} 199
kubelet_pleg_relist_interval_seconds_sum 210.0
kubelet_pleg_relist_interval_seconds_count 199
# HELP kubelet_pleg_last_seen_seconds [ALPHA] Timestamp in seconds when PLEG was last seen active.
# TYPE kubelet_pleg_last_seen_seconds gauge
kubelet_pleg_last_seen_seconds 1.7e9
kubelet_node_name{node="aks-latencypool-000"} 1
"""


# ---------- histogram quantile --------------------------------------------

def test_hist_quantile_interpolates_within_bucket():
    buckets = [(0.005, 195.0), (0.01, 197.0), (0.025, 200.0), (float("inf"), 200.0)]
    # p50 rank = 100 → falls in first bucket (cum 195), interpolated below 0.005
    assert kubelet_pleg._hist_quantile(buckets, 0.50) <= 0.005
    # p99 rank = 198 → falls in (0.01, 0.025] bucket
    p99 = kubelet_pleg._hist_quantile(buckets, 0.99)
    assert 0.01 < p99 <= 0.025


def test_hist_quantile_empty_returns_none():
    assert kubelet_pleg._hist_quantile([], 0.5) is None
    assert kubelet_pleg._hist_quantile([(0.005, 0.0), (float("inf"), 0.0)], 0.5) is None


# ---------- parser ---------------------------------------------------------

def test_parse_pleg_extracts_both_histograms():
    p = kubelet_pleg.parse_pleg(SAMPLE_KUBELET_METRICS)
    dur = p["relist_duration"]
    assert dur["count"] == 200
    assert abs(dur["avg_s"] - 0.0015) < 1e-9
    assert dur["p50_s"] <= 0.005
    assert 0.01 < dur["p99_s"] <= 0.025
    itv = p["relist_interval"]
    assert itv["count"] == 199
    assert abs(itv["avg_s"] - (210.0 / 199)) < 1e-9
    assert p["last_seen_unix_s"] == 1.7e9


def test_parse_pleg_ignores_non_pleg_metrics():
    p = kubelet_pleg.parse_pleg("kubelet_node_name{node=\"n\"} 1\n")
    assert p == {}


# ---------- persistence / columns -----------------------------------------

def test_pleg_lines_keeps_only_pleg():
    lines = kubelet_pleg._pleg_lines(SAMPLE_KUBELET_METRICS)
    assert "kubelet_pleg_relist_duration_seconds_count 200" in lines
    assert "kubelet_node_name" not in lines


def test_collect_persists_and_returns_headline(tmp_path):
    core = MagicMock(spec=client.CoreV1Api)
    core.connect_get_node_proxy_with_path.return_value = SAMPLE_KUBELET_METRICS
    out = kubelet_pleg.collect(core, node_name="n1", iter_dir=tmp_path)
    assert out["relist_duration"]["count"] == 200
    assert (tmp_path / "kubelet_pleg.txt").exists()
    saved = json.loads((tmp_path / "kubelet_pleg.json").read_text())
    assert saved["relist_interval"]["count"] == 199


def test_collect_returns_empty_when_proxy_fails(tmp_path):
    core = MagicMock(spec=client.CoreV1Api)
    core.connect_get_node_proxy_with_path.side_effect = client.ApiException(status=403)
    out = kubelet_pleg.collect(core, node_name="n1", iter_dir=tmp_path)
    assert out == {}
    assert not (tmp_path / "kubelet_pleg.txt").exists()


def test_collect_returns_empty_when_no_pleg_metrics(tmp_path):
    core = MagicMock(spec=client.CoreV1Api)
    core.connect_get_node_proxy_with_path.return_value = "some_other_metric 1\n"
    out = kubelet_pleg.collect(core, node_name="n1", iter_dir=tmp_path)
    assert out == {}


# ---------- record integration --------------------------------------------

def test_pleg_to_columns_null_when_absent():
    cols = kubelet_pleg.pleg_to_columns(None)
    assert set(cols) == set(kubelet_pleg.PLEG_COLUMNS)
    assert all(v is None for v in cols.values())


def test_record_to_row_includes_pleg_columns():
    rec = IterationRecord(iteration=1, run_id="20260101-000000",
                          provider="aks_kubenet", region="uksouth")
    rec.kubelet_pleg = kubelet_pleg.parse_pleg(SAMPLE_KUBELET_METRICS)
    row = rec.to_row()
    assert row["kubelet_pleg_relist_count"] == 200
    assert abs(row["kubelet_pleg_relist_avg_s"] - 0.0015) < 1e-9
    assert row["kubelet_pleg_interval_p50_s"] is not None
