"""Matplotlib plots for a single run, and --compare overlays across runs."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .analysis import METRICS

PHASE_COLS = [
    ("T0_pod_created", "T1_node_registered", "pod \u2192 node registered"),
    ("T1_node_registered", "T2_cilium_started", "node registered \u2192 cilium started"),
    ("T2_cilium_started", "T3_cilium_ready", "cilium init"),
    ("T3_cilium_ready", "T4_node_ready", "cilium ready \u2192 node ready"),
]


def _seconds(a: pd.Series, b: pd.Series) -> pd.Series:
    return (pd.to_datetime(a, utc=True, errors="coerce")
            - pd.to_datetime(b, utc=True, errors="coerce")).dt.total_seconds()


def _ok(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["status"] == "success"].reset_index(drop=True)


def plot_all(iterations_csv: Path, out_dir: Path, *, title: str = "") -> list[Path]:
    df = pd.read_csv(iterations_csv)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    ok = _ok(df)
    if ok.empty:
        return paths

    metrics_df = ok[[c for c in METRICS if c in ok.columns]].apply(pd.to_numeric, errors="coerce")

    # 1. box plot
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.boxplot([metrics_df[m].dropna() for m in metrics_df.columns],
               tick_labels=[m.replace("_s", "") for m in metrics_df.columns])
    ax.set_ylabel("seconds")
    ax.set_title(f"Latency distribution {title}")
    ax.grid(True, axis="y", alpha=0.3)
    p = out_dir / "box.png"; fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig); paths.append(p)

    # 2. mean + stddev bar
    fig, ax = plt.subplots(figsize=(9, 5))
    means = metrics_df.mean()
    stds = metrics_df.std(ddof=1).fillna(0.0)
    ax.bar(range(len(means)), means.values, yerr=stds.values, capsize=4)
    ax.set_xticks(range(len(means)))
    ax.set_xticklabels([m.replace("_s", "") for m in means.index], rotation=15, ha="right")
    ax.set_ylabel("seconds"); ax.set_title(f"Mean \u00b1 stddev {title}")
    ax.grid(True, axis="y", alpha=0.3)
    p = out_dir / "mean_stddev.png"; fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig); paths.append(p)

    # 3. stacked phase per iteration
    phases = pd.DataFrame({label: _seconds(ok[b], ok[a]).clip(lower=0)
                           for a, b, label in PHASE_COLS
                           if a in ok.columns and b in ok.columns})
    if not phases.empty:
        fig, ax = plt.subplots(figsize=(10, 5))
        bottom = np.zeros(len(phases))
        x = np.arange(1, len(phases) + 1)
        for col in phases.columns:
            vals = phases[col].fillna(0).values
            ax.bar(x, vals, bottom=bottom, label=col)
            bottom += vals
        ax.set_xlabel("iteration"); ax.set_ylabel("seconds")
        ax.set_title(f"Phase breakdown per iteration {title}")
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, axis="y", alpha=0.3)
        p = out_dir / "phase_stacked.png"; fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig); paths.append(p)

    # 4. latency vs iteration
    if "node_startup_latency_s" in metrics_df:
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.plot(range(1, len(metrics_df) + 1), metrics_df["node_startup_latency_s"], marker="o")
        ax.set_xlabel("iteration"); ax.set_ylabel("seconds")
        ax.set_title(f"Node startup latency vs iteration {title}")
        ax.grid(True, alpha=0.3)
        p = out_dir / "latency_vs_iteration.png"; fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig); paths.append(p)

    # 5. CDF
    s = metrics_df.get("node_startup_latency_s")
    if s is not None and s.dropna().size:
        s = s.dropna().sort_values().reset_index(drop=True)
        cdf = (np.arange(1, len(s) + 1)) / len(s)
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.plot(s.values, cdf, marker=".")
        for q, label in [(0.50, "p50"), (0.90, "p90"), (0.99, "p99")]:
            v = float(s.quantile(q))
            ax.axvline(v, linestyle="--", alpha=0.4)
            ax.text(v, q, f" {label}={v:.1f}s", va="center")
        ax.set_xlabel("node startup latency (s)"); ax.set_ylabel("CDF")
        ax.set_title(f"CDF of node startup latency {title}")
        ax.grid(True, alpha=0.3); ax.set_ylim(0, 1.02)
        p = out_dir / "cdf.png"; fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig); paths.append(p)

    return paths


def plot_compare(csvs: list[Path], out_dir: Path) -> list[Path]:
    """Overlay node-startup-latency CDF across multiple runs (cross-provider compare)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 5))
    for csv in csvs:
        df = pd.read_csv(csv)
        ok = _ok(df)
        s = pd.to_numeric(ok.get("node_startup_latency_s"), errors="coerce").dropna().sort_values()
        if s.empty:
            continue
        cdf = np.arange(1, len(s) + 1) / len(s)
        label = csv.parent.name
        ax.plot(s.values, cdf, marker=".", label=label)
    ax.set_xlabel("node startup latency (s)"); ax.set_ylabel("CDF")
    ax.set_title("Node startup latency CDF — comparison")
    ax.grid(True, alpha=0.3); ax.legend()
    p = out_dir / "compare_cdf.png"; fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig)
    return [p]
