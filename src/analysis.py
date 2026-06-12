"""Aggregation + summary emission."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pandas as pd
from tabulate import tabulate

from .records import IterationRecord


# Trigger-pod lifecycle decomposition. All seconds, anchored within the
# [T_trigger_scheduled, T5_pod_running] window so they sum (≈) to
# `sandbox_setup_s`. Derived from the per-iteration JSON columns
# (node_image_pulls_json / node_container_creates_json /
# node_container_starts_json) filtered to the trigger pod by pod_name.
TRIGGER_POD_METRICS = [
    # Scheduler bind → first kubelet action on the trigger pod (sandbox
    # create / volume mount / CNI ADD). Time before any container-level
    # event fires. Captures the cost of pod sandbox setup.
    "trigger_prepull_s",
    # Last `Pulling`..`Pulled` window across the trigger pod's containers.
    # 0 when the image was cached on the node (no Pulling event).
    "trigger_image_pull_s",
    # `Pulled` → `Created` — kubelet CRI CreateContainer roundtrip.
    "trigger_create_s",
    # `Created` → `Started` — kubelet StartContainer + entrypoint hand-off.
    "trigger_run_gap_s",
    # T5_pod_running − T_trigger_scheduled. Canonical total for the
    # window (== sandbox_setup_s). Surfaced as its own metric so plot /
    # summary code can drive directly off `trigger_total_s` rather than
    # the legacy name.
    "trigger_total_s",
]


def _safe_json_list(val) -> list:
    if val is None:
        return []
    if isinstance(val, float) and pd.isna(val):
        return []
    if isinstance(val, list):
        return val
    try:
        out = json.loads(val)
        return out if isinstance(out, list) else []
    except Exception:
        return []


def _max_ts(seq):
    vals = [pd.to_datetime(x, utc=True, errors="coerce") for x in seq if x]
    vals = [v for v in vals if pd.notna(v)]
    return max(vals) if vals else None


def _min_ts(seq):
    vals = [pd.to_datetime(x, utc=True, errors="coerce") for x in seq if x]
    vals = [v for v in vals if pd.notna(v)]
    return min(vals) if vals else None


def _seconds_between(a, b) -> float | None:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return None
    return max((a - b).total_seconds(), 0.0)


def enrich_trigger_pod_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Add per-iteration trigger-pod lifecycle decomposition columns in place.

    Decomposes the [T_trigger_scheduled, T5_pod_running] window into
    ``trigger_{prepull,image_pull,create,run_gap}_s`` plus the canonical
    ``trigger_total_s``. Idempotent: existing non-null values are preserved
    so this can run on both fresh and legacy iterations.csv files.
    """
    for c in TRIGGER_POD_METRICS:
        if c not in df.columns:
            df[c] = pd.NA
    if df.empty:
        return df

    sched = pd.to_datetime(df.get("T_trigger_scheduled"), utc=True, errors="coerce") \
        if "T_trigger_scheduled" in df.columns else pd.Series([pd.NaT] * len(df))
    t5 = pd.to_datetime(df.get("T5_pod_running"), utc=True, errors="coerce") \
        if "T5_pod_running" in df.columns else pd.Series([pd.NaT] * len(df))

    pulls_col = df["node_image_pulls_json"] if "node_image_pulls_json" in df.columns \
        else pd.Series([None] * len(df))
    creates_col = df["node_container_creates_json"] if "node_container_creates_json" in df.columns \
        else pd.Series([None] * len(df))
    starts_col = df["node_container_starts_json"] if "node_container_starts_json" in df.columns \
        else pd.Series([None] * len(df))
    pod_col = df["pod_name"] if "pod_name" in df.columns else pd.Series([None] * len(df))

    out: dict[str, list] = {c: [] for c in TRIGGER_POD_METRICS}
    for i in range(len(df)):
        pod = pod_col.iat[i]
        s_sched = sched.iat[i]
        s_run = t5.iat[i]
        total = _seconds_between(s_run, s_sched)

        pulls = _safe_json_list(pulls_col.iat[i])
        creates = _safe_json_list(creates_col.iat[i])
        starts = _safe_json_list(starts_col.iat[i])

        # Filter to the trigger pod's main (non-init) containers.
        mine = lambda evs, key: [e for e in evs
                                 if (e.get("pod") == pod) and not e.get(key, False)]
        my_pulls = mine(pulls, "failed") if pulls else []
        # Pulls don't carry an `init` flag — keep all that match the pod.
        my_pulls = [p for p in pulls if p.get("pod") == pod]
        my_creates = [c for c in creates if c.get("pod") == pod and not c.get("init", False)]
        my_starts = [s for s in starts if s.get("pod") == pod and not s.get("init", False)]

        pull_start = _min_ts(p.get("t_pulling") for p in my_pulls)
        pull_end = _max_ts(p.get("t_pulled") for p in my_pulls)
        create_end = _max_ts(c.get("t_created") for c in my_creates)
        start_end = _max_ts(s.get("t_started") for s in my_starts)

        # Phase math, with sensible fallbacks when an event is missing.
        # Anchor "first kubelet action" at the earliest available signal.
        first_action = _min_ts([pull_start, pull_end, create_end, start_end, s_run])
        prepull = _seconds_between(first_action, s_sched)
        pull = _seconds_between(pull_end, pull_start) if pull_start and pull_end else 0.0
        create = _seconds_between(create_end, pull_end or first_action) if create_end else None
        run_gap = _seconds_between(start_end or s_run, create_end) if create_end else None

        # Reconcile to total when we have it: if the per-phase sum overshoots
        # (e.g. overlapping events) or undershoots (missing events) collapse
        # the residual into prepull so the bar always sums to total. Total
        # itself is the authoritative number.
        parts = [v for v in (prepull, pull, create, run_gap) if v is not None]
        if total is not None and parts:
            known = sum(v for v in (pull, create, run_gap) if v is not None)
            if known <= total:
                prepull = max(total - known, 0.0)
            else:
                # Phases overshoot total (clock skew / overlap) — normalise.
                scale = total / known if known > 0 else 0.0
                pull = (pull or 0.0) * scale
                create = (create or 0.0) * scale if create is not None else None
                run_gap = (run_gap or 0.0) * scale if run_gap is not None else None
                prepull = max(total - sum(v for v in (pull, create, run_gap) if v is not None), 0.0)

        out["trigger_prepull_s"].append(prepull)
        out["trigger_image_pull_s"].append(pull if pull is not None else (0.0 if total is not None else None))
        out["trigger_create_s"].append(create)
        out["trigger_run_gap_s"].append(run_gap)
        out["trigger_total_s"].append(total)

    for c in TRIGGER_POD_METRICS:
        new_vals = pd.to_numeric(pd.Series(out[c]), errors="coerce")
        existing = pd.to_numeric(df[c], errors="coerce")
        # Preserve any pre-existing non-null values; backfill the rest.
        df[c] = existing.where(existing.notna(), new_vals.values)
    return df

METRICS = [
    # ---- Headline (K8s-networking) — anchored at T1 to exclude IaaS noise ----
    # `time_to_runnable_s` (T5 − T1) is the recommended lead metric for
    # cross-provider comparison: it captures everything kubelet + CNI + the
    # cilium agent + IPAM do between node-registered and the workload pod
    # transitioning to Running, with the cloud-side autoscaler + VM bringup
    # (T0 → T1) explicitly excluded.
    "time_to_runnable_s",
    "T1c_s_from_T1",
    "T2_s_from_T1",
    "T3_s_from_T1",
    "T4_s_from_T1",
    "T4b_s_from_T1",
    "T5_s_from_T1",
    "sandbox_setup_s",
    # ---- Trigger-pod lifecycle decomposition (within sandbox_setup window) ----
    "trigger_prepull_s",
    "trigger_image_pull_s",
    "trigger_create_s",
    "trigger_run_gap_s",
    "trigger_total_s",
    # ---- Legacy / supporting (T0-anchored or component-level) ----
    "node_startup_latency_s",
    "time_to_schedulable_s",
    "node_register_latency_s",
    "node_ready_after_register_s",
    "cni_conflist_install_s",
    "pod_scheduling_lag_s",
    "image_pull_s",
    "csinode_block_s",
    "taint_observed_offset_s",
    "post_conflist_ready_s",
    "cilium_init_duration_s",
    "cni_induced_delay_s",
    "cilium_scheduling_block_s",
    # Tier-1 deep-Cilium headlines — silently skipped when the column is
    # absent or all-null (i.e. --deep-cilium not set).
    "cilium_bootstrap_total_s",
    "cilium_bootstrap_early_init_s",
    "cilium_bootstrap_k8s_init_s",
    "cilium_bootstrap_daemon_init_s",
    "cilium_bootstrap_ipam_s",
    "cilium_bootstrap_maps_init_s",
    "cilium_bootstrap_bpf_base_s",
    "cilium_bootstrap_restore_s",
    "cilium_bootstrap_cleanup_s",
    "cilium_bootstrap_fqdn_s",
    "cilium_bootstrap_enable_conntrack_s",
    "cilium_bootstrap_health_check_s",
    "cilium_endpoint_regen_avg_s",
    "cilium_endpoint_regen_bpf_compilation_s",
    "cilium_endpoint_regen_bpf_wait_for_elf_s",
    "cilium_endpoint_regen_bpf_load_prog_s",
    "cilium_endpoint_regen_waiting_for_lock_s",
    "cilium_endpoint_regen_map_sync_s",
]

# Metrics to feature in the headline "K8s networking" table in summary.md.
# All are T1-anchored so the IaaS-side variance (T0 -> T1) is excluded.
HEADLINE_METRICS = [
    "time_to_runnable_s",
    "T1c_s_from_T1",
    "T2_s_from_T1",
    "T3_s_from_T1",
    "T4_s_from_T1",
    "T4b_s_from_T1",
    "T5_s_from_T1",
    "sandbox_setup_s",
]


def to_dataframe(records: Iterable[IterationRecord]) -> pd.DataFrame:
    df = pd.DataFrame([r.to_row() for r in records])
    return enrich_trigger_pod_metrics(df)


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    df = enrich_trigger_pod_metrics(df)
    rows = []
    for m in METRICS:
        if m not in df.columns:
            continue
        s = pd.to_numeric(df[m], errors="coerce").dropna()
        if s.empty:
            # Skip silently for optional deep-cilium columns; keep the
            # placeholder for the always-present core metrics.
            if m.startswith("cilium_bootstrap_") or m == "cilium_endpoint_regen_avg_s":
                continue
            rows.append({"metric": m, "count": 0})
            continue
        # Use median + IQR (p25/p50/p75) alongside mean+stddev because
        # the IaaS-side T0->T1 variance can make mean+stddev misleading
        # (e.g. GKE Autopilot cold-pool runs span 7-170s on T1). Median
        # + IQR is more robust to those tails and is what summary.md
        # leads with for K8s-networking metrics.
        rows.append({
            "metric": m,
            "count": int(s.count()),
            "mean": round(float(s.mean()), 3),
            "stddev": round(float(s.std(ddof=1)) if s.count() > 1 else 0.0, 3),
            "min": round(float(s.min()), 3),
            "p25": round(float(s.quantile(0.25)), 3),
            "p50": round(float(s.quantile(0.50)), 3),
            "p75": round(float(s.quantile(0.75)), 3),
            "p90": round(float(s.quantile(0.90)), 3),
            "p99": round(float(s.quantile(0.99)), 3),
            "max": round(float(s.max()), 3),
        })
    return pd.DataFrame(rows)


def write_outputs(records: list[IterationRecord], out_dir: Path,
                  *, run_id: str, provider: str, region: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    df = to_dataframe(records)
    df.to_csv(out_dir / "iterations.csv", index=False)

    summary = aggregate(df)
    summary.to_csv(out_dir / "summary.csv", index=False)

    # Headline (K8s-networking) view — T1-anchored, IaaS-noise excluded.
    headline = summary[summary["metric"].isin(HEADLINE_METRICS)].copy()
    if not headline.empty and "p50" in headline.columns:
        # Order rows in the canonical lifecycle order rather than METRICS order.
        headline["__ord"] = headline["metric"].map(
            {m: i for i, m in enumerate(HEADLINE_METRICS)})
        headline = headline.sort_values("__ord").drop(columns="__ord")
        headline_cols = [c for c in ["metric", "count", "p25", "p50", "p75", "mean", "stddev"]
                         if c in headline.columns]
        headline_view = headline[headline_cols]
    else:
        headline_view = headline

    md_lines = [
        f"# Node Startup Latency — Run `{run_id}`",
        "",
        f"- Provider: **{provider}**",
        f"- Region: **{region}**",
        f"- Iterations: **{len(df)}** (success: {(df['status'] == 'success').sum()})",
        "",
        "## K8s networking (T1-anchored, IaaS-noise excluded)",
        "",
        "Headline metric: `time_to_runnable_s` = T5_pod_running − T1_node_registered.",
        "This is the only number that's apples-to-apples across providers — it",
        "excludes cloud autoscaler / VM bringup variance (T0 → T1) and isolates",
        "the cost of kubelet + CNI + cilium agent + IPAM wiring on a fresh node.",
        "Use **p50** (median) as the comparison point; p25/p75 show the spread.",
        "",
        tabulate(headline_view, headers="keys", tablefmt="github",
                 showindex=False, floatfmt=".2f"),
        "",
        "## All metrics (median + IQR + mean ± stddev)",
        "",
        tabulate(summary, headers="keys", tablefmt="github", showindex=False),
        "",
        "## Per-iteration",
        "",
        tabulate(df[[
            "iteration", "node_name", "status",
            "time_to_runnable_s", "T1c_s_from_T1", "T2_s_from_T1",
            "T3_s_from_T1", "T4_s_from_T1", "T4b_s_from_T1", "T5_s_from_T1",
            "sandbox_setup_s",
            "node_startup_latency_s", "node_register_latency_s",
        ]], headers="keys", tablefmt="github", showindex=False, floatfmt=".2f"),
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(md_lines))

    json_summary = {
        "run_id": run_id,
        "provider": provider,
        "region": region,
        "iterations": len(df),
        "successful": int((df["status"] == "success").sum()),
        "metrics": summary.to_dict(orient="records"),
    }
    (out_dir / "summary.json").write_text(json.dumps(json_summary, indent=2))
    return json_summary
