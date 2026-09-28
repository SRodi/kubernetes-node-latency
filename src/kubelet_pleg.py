"""Tier-1 kubelet PLEG capture — scrape the new node's kubelet `/metrics`.

The container-runtime-independent "PLEG delay" derived elsewhere from
Kubernetes Events / container `startedAt` is floored at ~1 s by Event
timestamp resolution. The authoritative sub-second signal is the kubelet's
own PLEG (Pod Lifecycle Event Generator) instrumentation:

    kubelet_pleg_relist_duration_seconds   histogram — how long each relist
                                           (CRI list + diff) takes; the
                                           canonical "PLEG health" metric.
    kubelet_pleg_relist_interval_seconds   histogram — wall-clock gap between
                                           consecutive relists (≈ 1 s cadence
                                           when healthy; grows under pressure).
    kubelet_pleg_last_seen_seconds         gauge — unix time of last relist.

These are cumulative since kubelet start, so by the time a fresh node reaches
`Ready` (T4) it already carries tens of relist samples covering the exact
window in which pods were being wired up.

Transport: the kubelet `/metrics` endpoint is authenticated (HTTPS :10250),
so instead of a scraper Pod (as `cilium_deep` uses for the agent's PodIP) we
proxy through the apiserver — `GET /api/v1/nodes/<node>/proxy/metrics` — which
the run's admin kubeconfig is authorised for on GKE / AKS / EKS alike and
needs no shell, no privileged Pod, and works on kubenet nodes with no CNI
agent at all. Best-effort: never raises; a failed scrape just yields null
columns for that iteration.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kubernetes import client

from .cilium_deep import _iter_samples  # reuse the Prometheus line parser

log = logging.getLogger(__name__)

# PLEG metric family names we persist / parse.
_RELIST_DURATION = "kubelet_pleg_relist_duration_seconds"
_RELIST_INTERVAL = "kubelet_pleg_relist_interval_seconds"
_LAST_SEEN = "kubelet_pleg_last_seen_seconds"


def fetch_kubelet_metrics(core: client.CoreV1Api, node_name: str,
                          *, timeout_s: int = 30) -> str | None:
    """Return the kubelet `/metrics` text for `node_name` via the apiserver
    node proxy, or None on any failure.
    """
    try:
        return core.connect_get_node_proxy_with_path(
            name=node_name, path="metrics",
            _request_timeout=(10, timeout_s),
        )
    except client.ApiException as e:
        log.info("kubelet metrics proxy failed for %s (%s): %s",
                 node_name, getattr(e, "status", "?"), getattr(e, "reason", e))
        return None
    except Exception as e:  # noqa: BLE001 — connection/timeout must never break the run
        log.info("kubelet metrics proxy error for %s: %s", node_name, e)
        return None


def _hist_quantile(buckets: list[tuple[float, float]], q: float) -> float | None:
    """Prometheus-style histogram_quantile over cumulative `(le, count)`
    buckets (must include the `+Inf` bucket). Linear-interpolates within the
    bucket the rank falls in. Returns None when the histogram is empty.
    """
    if not buckets:
        return None
    buckets = sorted(buckets, key=lambda b: b[0])
    total = buckets[-1][1]
    if total <= 0:
        return None
    rank = q * total
    prev_le = 0.0
    prev_count = 0.0
    for le, cum in buckets:
        if cum >= rank:
            if le == float("inf"):
                return prev_le
            if cum == prev_count:
                return le
            # interpolate within (prev_le, le] over the bucket's slice.
            frac = (rank - prev_count) / (cum - prev_count)
            return prev_le + (le - prev_le) * frac
        prev_le, prev_count = le, cum
    return buckets[-1][0]


def _parse_histogram(metrics_text: str, family: str) -> dict[str, Any] | None:
    """Extract avg / p50 / p90 / p99 / count for a single kubelet histogram
    family from a Prometheus dump. Kubelet PLEG histograms are unlabelled
    (one series per family), so labels are ignored.
    """
    buckets: list[tuple[float, float]] = []
    hsum: float | None = None
    hcount: float | None = None
    for name, labels, value in _iter_samples(metrics_text):
        if name == f"{family}_bucket":
            le = labels.get("le")
            if le is None:
                continue
            le_f = float("inf") if le in ("+Inf", "Inf") else float(le)
            buckets.append((le_f, value))
        elif name == f"{family}_sum":
            hsum = value
        elif name == f"{family}_count":
            hcount = value
    if hcount is None and not buckets:
        return None
    avg = (hsum / hcount) if (hsum is not None and hcount) else None
    return {
        "count": hcount,
        "avg_s": avg,
        "p50_s": _hist_quantile(buckets, 0.50),
        "p90_s": _hist_quantile(buckets, 0.90),
        "p99_s": _hist_quantile(buckets, 0.99),
    }


def parse_pleg(metrics_text: str) -> dict[str, Any]:
    """Parse the PLEG headline numbers out of a kubelet `/metrics` dump."""
    out: dict[str, Any] = {}
    dur = _parse_histogram(metrics_text, _RELIST_DURATION)
    if dur:
        out["relist_duration"] = dur
    itv = _parse_histogram(metrics_text, _RELIST_INTERVAL)
    if itv:
        out["relist_interval"] = itv
    for name, _labels, value in _iter_samples(metrics_text):
        if name == _LAST_SEEN:
            out["last_seen_unix_s"] = value
            break
    return out


def _pleg_lines(metrics_text: str) -> str:
    """Keep only PLEG-related lines (raw persistence, so we don't dump the
    kubelet's full multi-thousand-line metrics page per iteration)."""
    keep = []
    for line in metrics_text.splitlines():
        if line.startswith("#"):
            if "pleg" in line:
                keep.append(line)
        elif "kubelet_pleg" in line:
            keep.append(line)
    return "\n".join(keep) + "\n"


def collect(core: client.CoreV1Api, *, node_name: str,
            iter_dir: Path, timeout_s: int = 30) -> dict[str, Any]:
    """Scrape + parse the new node's kubelet PLEG metrics.

    Persists `kubelet_pleg.txt` (raw PLEG lines) and `kubelet_pleg.json`
    (parsed headline) under `iter_dir` only when something was captured.
    Returns the parsed headline dict (possibly empty) for merging into the
    IterationRecord. Best-effort: never raises.
    """
    headline: dict[str, Any] = {}
    raw = fetch_kubelet_metrics(core, node_name, timeout_s=timeout_s)
    if not raw or "kubelet_pleg" not in raw:
        return headline
    parsed = parse_pleg(raw)
    if not parsed:
        return headline
    iter_dir.mkdir(parents=True, exist_ok=True)
    try:
        (iter_dir / "kubelet_pleg.txt").write_text(_pleg_lines(raw))
        (iter_dir / "kubelet_pleg.json").write_text(
            json.dumps(parsed, indent=2, default=str))
    except OSError as e:
        log.warning("kubelet PLEG persist failed for %s: %s", node_name, e)
    return parsed


# ---- record integration ---------------------------------------------------

PLEG_COLUMNS = (
    "kubelet_pleg_relist_avg_s",
    "kubelet_pleg_relist_p50_s",
    "kubelet_pleg_relist_p90_s",
    "kubelet_pleg_relist_p99_s",
    "kubelet_pleg_relist_count",
    "kubelet_pleg_interval_avg_s",
    "kubelet_pleg_interval_p50_s",
    "kubelet_pleg_interval_p90_s",
    "kubelet_pleg_interval_p99_s",
)


def pleg_to_columns(headline: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten a parsed PLEG headline into flat iterations.csv columns."""
    if not headline:
        return {c: None for c in PLEG_COLUMNS}
    dur = headline.get("relist_duration") or {}
    itv = headline.get("relist_interval") or {}
    return {
        "kubelet_pleg_relist_avg_s":   dur.get("avg_s"),
        "kubelet_pleg_relist_p50_s":   dur.get("p50_s"),
        "kubelet_pleg_relist_p90_s":   dur.get("p90_s"),
        "kubelet_pleg_relist_p99_s":   dur.get("p99_s"),
        "kubelet_pleg_relist_count":   dur.get("count"),
        "kubelet_pleg_interval_avg_s": itv.get("avg_s"),
        "kubelet_pleg_interval_p50_s": itv.get("p50_s"),
        "kubelet_pleg_interval_p90_s": itv.get("p90_s"),
        "kubelet_pleg_interval_p99_s": itv.get("p99_s"),
    }
