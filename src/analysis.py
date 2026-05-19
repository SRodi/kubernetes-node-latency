"""Aggregation + summary emission."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pandas as pd
from tabulate import tabulate

from .records import IterationRecord

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
    return pd.DataFrame([r.to_row() for r in records])


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
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
