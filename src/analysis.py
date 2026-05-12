"""Aggregation + summary emission."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pandas as pd
from tabulate import tabulate

from .records import IterationRecord

METRICS = [
    "node_startup_latency_s",
    "node_register_latency_s",
    "cilium_init_duration_s",
    "cni_induced_delay_s",
    # Tier-1 deep-Cilium headlines — silently skipped when the column is
    # absent or all-null (i.e. --deep-cilium not set).
    "cilium_bootstrap_total_s",
    "cilium_bootstrap_k8s_init_s",
    "cilium_bootstrap_restore_s",
    "cilium_bootstrap_bpf_base_s",
    "cilium_bootstrap_ipam_s",
    "cilium_bootstrap_proxy_s",
    "cilium_endpoint_regen_avg_s",
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
        rows.append({
            "metric": m,
            "count": int(s.count()),
            "mean": round(float(s.mean()), 3),
            "stddev": round(float(s.std(ddof=1)) if s.count() > 1 else 0.0, 3),
            "min": round(float(s.min()), 3),
            "p50": round(float(s.quantile(0.50)), 3),
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

    md_lines = [
        f"# Node Startup Latency — Run `{run_id}`",
        "",
        f"- Provider: **{provider}**",
        f"- Region: **{region}**",
        f"- Iterations: **{len(df)}** (success: {(df['status'] == 'success').sum()})",
        "",
        "## Aggregate (seconds)",
        "",
        tabulate(summary, headers="keys", tablefmt="github", showindex=False),
        "",
        "## Per-iteration",
        "",
        tabulate(df[[
            "iteration", "node_name", "status",
            "node_startup_latency_s", "node_register_latency_s",
            "cilium_init_duration_s", "cni_induced_delay_s",
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
