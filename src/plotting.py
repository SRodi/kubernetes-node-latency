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
    ("T0_pod_created", "T1_node_registered", "VM provision + node registered"),
    ("T1_node_registered", "T4_node_ready", "node init to Ready"),
]


# Profile lanes: each entry is (lane label, start column, end column, actor key).
# `actor` controls the bar colour so the reader can immediately see which
# concurrent processes are happening on the same time-window.
PROFILE_LANES = [
    ("Cloud / autoscaler + VM bringup",      "T0_pod_created",     "T1_node_registered", "cloud"),
    # T1 \u2192 T1c: kubelet reports NetworkPluginNotReady until the CNI plugin
    # writes its conflist into /etc/cni/net.d/. Both views (kubelet block,
    # CNI install-cni progress) describe the same wall-clock window, so we
    # render a single lane here.
    ("CNI install-cni \u2192 conflist placed (kubelet blocked on CNI)",
                                              "T1_node_registered", "T1c_cni_conflist",   "cni"),
    ("Kubelet: residual status sync",        "T1c_cni_conflist",   "T4_node_ready",      "kubelet"),
    ("Cilium agent container running",       "T2_cilium_started",  "T3_cilium_ready",    "cilium"),
    ("Scheduling block (cilium taint)",      "T4_node_ready",      "T4b_schedulable",    "scheduler"),
]

ACTOR_COLORS = {
    "cloud":         "#9aa0a6",  # grey
    "kubelet":       "#4285f4",  # blue
    "cni":           "#fb8c00",  # orange
    "cilium":        "#34a853",  # green
    "cilium_sub":    "#1b7a36",  # darker green for internal sub-phases
    "scheduler":     "#ea4335",  # red
}

# Cilium bootstrap sub-phases in the canonical execution order published by
# `cilium_bootstrap_seconds`. Each entry maps a lane label → iterations.csv
# column. Only phases with non-trivial mean duration (> ~5 ms) are plotted.
CILIUM_BOOTSTRAP_PHASES = [
    ("bootstrap.earlyInit",       "cilium_bootstrap_early_init_s"),
    ("bootstrap.k8sInit",         "cilium_bootstrap_k8s_init_s"),
    ("bootstrap.daemonInit",      "cilium_bootstrap_daemon_init_s"),
    ("bootstrap.ipam",            "cilium_bootstrap_ipam_s"),
    ("bootstrap.mapsInit",        "cilium_bootstrap_maps_init_s"),
    ("bootstrap.bpfBase",         "cilium_bootstrap_bpf_base_s"),
    ("bootstrap.restore",         "cilium_bootstrap_restore_s"),
    ("bootstrap.cleanup",         "cilium_bootstrap_cleanup_s"),
    ("bootstrap.fqdn",            "cilium_bootstrap_fqdn_s"),
    ("bootstrap.enableConntrack", "cilium_bootstrap_enable_conntrack_s"),
    ("bootstrap.healthCheck",     "cilium_bootstrap_health_check_s"),
]

# Endpoint-regeneration sub-phases. These are *per-endpoint averages* (not
# wall-clock), so they're rendered as a separate annotation lane.
CILIUM_REGEN_PHASES = [
    ("regen.bpfCompilation",    "cilium_endpoint_regen_bpf_compilation_s"),
    ("regen.bpfWaitForELF",     "cilium_endpoint_regen_bpf_wait_for_elf_s"),
    ("regen.bpfLoadProg",       "cilium_endpoint_regen_bpf_load_prog_s"),
    ("regen.waitingForLock",    "cilium_endpoint_regen_waiting_for_lock_s"),
    ("regen.mapSync",           "cilium_endpoint_regen_map_sync_s"),
]


def _seconds(a: pd.Series, b: pd.Series) -> pd.Series:
    return (pd.to_datetime(a, utc=True, errors="coerce")
            - pd.to_datetime(b, utc=True, errors="coerce")).dt.total_seconds()


def _ok(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["status"] == "success"].reset_index(drop=True)


def plot_all(iterations_csv: Path, out_dir: Path, *, title: str = "") -> list[Path]:
    df = pd.read_csv(iterations_csv)
    # Re-parse cilium_deep_headline.json files alongside the CSV so that
    # iterations.csv files predating the expanded bootstrap schema still get
    # the new sub-phase columns populated for plotting.
    _backfill_cilium_deep(df, iterations_csv.parent)
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

    # 3. stacked phase per iteration (always sums to node_startup_latency_s = T4 - T0).
    #    Cilium agent init (T3 - T2) is overlaid as a separate line because it
    #    can complete BEFORE or AFTER T4 (e.g. on GKE Autopilot it lands after).
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
        cilium = pd.to_numeric(ok.get("cilium_init_duration_s"), errors="coerce")
        if cilium is not None and cilium.notna().any():
            ax.plot(x, cilium.values, color="black", marker="D", linewidth=1.2,
                    label="cilium agent init (T3 \u2212 T2, parallel)")
        ax.set_xlabel("iteration"); ax.set_ylabel("seconds")
        ax.set_title(
            f"Phase breakdown per iteration {title}\n"
            "stack = node startup latency (T4 \u2212 T0); diamonds = cilium init duration"
        )
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

    # 6. profile / Gantt: each phase as a swimlane on a shared time axis so the
    #    reader can see at a glance which lifecycle steps overlap in time
    #    (vertically stacked = parallel) and which are sequential.
    p = _plot_phase_profile(ok, out_dir, title=title)
    if p is not None:
        paths.append(p)

    return paths


def _plot_phase_profile(ok: pd.DataFrame, out_dir: Path, *, title: str) -> Path | None:
    """Per-run mean-aligned Gantt of every captured phase."""
    if ok.empty:
        return None

    def _mean_offset(col: str) -> float | None:
        if col not in ok.columns:
            return None
        s = _seconds(ok[col], ok["T0_pod_created"])
        s = s[s.notna()]
        if s.empty:
            return None
        return float(s.mean())

    lanes: list[tuple[str, float, float, str]] = []
    for label, start_col, end_col, actor in PROFILE_LANES:
        if actor == "cloud":
            continue  # rendered as a numeric annotation, not a bar
        s_off = _mean_offset(start_col)
        e_off = _mean_offset(end_col)
        if s_off is None or e_off is None:
            continue
        dur = max(e_off - s_off, 0.0)
        if dur <= 1e-3 and label != "Scheduling block (cilium taint)":
            continue
        lanes.append((label, s_off, e_off, actor))

    if not lanes:
        return None

    # Re-baseline x-axis to T1 (node registered) so the much smaller post-T1
    # phases are readable. The autoscaler/VM bringup duration is shown as a
    # numeric annotation in the title instead.
    t1_off = _mean_offset("T1_node_registered") or 0.0
    cloud_dur = t1_off  # T1 - T0
    lanes = [(label, s_off - t1_off, e_off - t1_off, actor)
             for (label, s_off, e_off, actor) in lanes]

    # ---- Cilium bootstrap sub-phases (only when --deep-cilium data exists) ----
    # Bootstrap timings are durations only, not absolute timestamps, so we
    # chain them sequentially ending at T3 (when the agent reports ready):
    #   start_of_first_phase = T3 - sum(phase_means)
    # The pre-bootstrap remainder of (T3 - T2) is image-pull / container-start
    # / readiness-probe lag and is rendered as a leading "image-pull / cont
    # start" sub-segment.
    bootstrap_means: list[tuple[str, float]] = []
    for label, col in CILIUM_BOOTSTRAP_PHASES:
        m = _mean_offset_metric(ok, col)
        if m is None or m < 5e-3:  # < 5 ms — not visually meaningful
            continue
        bootstrap_means.append((label, m))

    # Single composite lane with chained colored segments — far more readable
    # at the wall-clock scale than one lane per phase. Image-pull /
    # container-start is rendered as a separate light-grey leading segment so
    # the bootstrap phases get plenty of zoom width on the right.
    cilium_breakdown: list[tuple[str, float, float]] = []  # (label, start, end)
    pre_bs_dur = 0.0
    t2_off = _mean_offset("T2_cilium_started")
    t3_off = _mean_offset("T3_cilium_ready")
    if bootstrap_means and t2_off is not None and t3_off is not None:
        bootstrap_sum = sum(m for _, m in bootstrap_means)
        bs_start = (t3_off - t1_off) - bootstrap_sum
        pre_bs_dur = max(bs_start - (t2_off - t1_off), 0.0)
        cursor = bs_start
        for label, dur in bootstrap_means:
            short = label.replace("bootstrap.", "")
            cilium_breakdown.append((short, cursor, cursor + dur))
            cursor += dur

    all_lanes = lanes

    markers: list[tuple[str, float]] = []
    for col, glyph in [
        ("T1_node_registered", "T1"),
        ("T1c_cni_conflist", "T1c"),
        ("T2_cilium_started", "T2"),
        ("T3_cilium_ready", "T3"),
        ("T4_node_ready", "T4"),
        ("T4b_schedulable", "T4b"),
    ]:
        off = _mean_offset(col)
        if off is not None:
            markers.append((glyph, off - t1_off))

    # Layout: main wall-clock Gantt on top, optional zoomed Cilium-internal
    # breakdown below (only when --deep-cilium data is available).
    has_breakdown = bool(cilium_breakdown)
    n_main = len(all_lanes)
    fig_height = 4 + 0.35 * n_main + (2.0 if has_breakdown else 0)
    if has_breakdown:
        fig, (ax, ax_bd) = plt.subplots(
            2, 1, figsize=(12, fig_height),
            gridspec_kw={"height_ratios": [max(n_main, 4), 2.0]},
        )
    else:
        fig, ax = plt.subplots(figsize=(12, fig_height))
        ax_bd = None

    y_positions = np.arange(n_main, 0, -1)  # top-down listing
    used_actors: list[str] = []
    for (label, s_off, e_off, actor), y in zip(all_lanes, y_positions):
        dur = e_off - s_off
        color = ACTOR_COLORS.get(actor, "#888888")
        ax.barh(y, max(dur, 0.05), left=s_off, height=0.55, color=color,
                edgecolor="black", linewidth=0.5)
        text = f"{dur:.2f}s"
        ax.text(s_off + dur / 2 if dur > 1.5 else e_off + 0.4, y, text,
                va="center", ha="center" if dur > 1.5 else "left",
                fontsize=8, color="white" if dur > 1.5 else "black")
        if actor not in used_actors:
            used_actors.append(actor)

    for glyph, off in markers:
        ax.axvline(off, color="black", linestyle=":", alpha=0.35, linewidth=0.8)
        ax.text(off, n_main + 0.6, glyph, ha="center", va="bottom",
                fontsize=8, color="black")

    ax.set_yticks(y_positions)
    ax.set_yticklabels([label for label, _, _, _ in all_lanes], fontsize=9)
    ax.set_xlabel("seconds since T1 (node registered)")
    ax.set_ylim(0.2, n_main + 1.2)

    regen_bits: list[str] = []
    for label, col in CILIUM_REGEN_PHASES:
        m = _mean_offset_metric(ok, col)
        if m is not None and m >= 5e-3:
            regen_bits.append(f"{label.replace('regen.','')}={m:.2f}s")
    regen_note = (
        "endpoint-regen avg (per-endpoint, post-T3): " + ", ".join(regen_bits)
    ) if regen_bits else ""

    title_lines = [
        f"Phase profile {title}",
        f"Cloud / autoscaler + VM bringup (T0\u2192T1): {cloud_dur:.2f}s (not shown)",
    ]
    if regen_note:
        title_lines.append(regen_note)
    title_lines.append("bars overlapping on x = parallel; back-to-back = sequential.")
    ax.set_title("\n".join(title_lines), fontsize=9, loc="center")
    ax.grid(True, axis="x", alpha=0.3)

    from matplotlib.patches import Patch
    legend_handles = [
        Patch(facecolor=ACTOR_COLORS[a], edgecolor="black", label=a)
        for a in used_actors
    ]
    ax.legend(handles=legend_handles, loc="lower right", fontsize=8, title="actor")

    # ---- Cilium internal breakdown (zoomed) ----
    if has_breakdown and ax_bd is not None:
        # palette for distinguishable segments; cycle if more than 10 phases
        palette = ["#a8d5a8", "#85c785", "#5fb35f", "#3e9b3e", "#1f7a1f",
                   "#107510", "#0e6b0e", "#0c620c", "#0a580a", "#084e08"]
        bd_start = cilium_breakdown[0][1]
        bd_end = cilium_breakdown[-1][2]
        bd_total = bd_end - bd_start
        for i, (label, s, e) in enumerate(cilium_breakdown):
            dur = e - s
            color = palette[i % len(palette)]
            ax_bd.barh(0.5, dur, left=s, height=0.7, color=color,
                       edgecolor="black", linewidth=0.4)
            rel = dur / bd_total if bd_total > 0 else 0
            inline = rel > 0.20
            txt = f"{label}\n{dur*1000:.0f}ms" if dur < 0.5 else f"{label}\n{dur:.2f}s"
            # Stagger above-bar labels to avoid overlap when bootstrap phases
            # are crammed together.
            if inline:
                ax_bd.text(s + dur / 2, 0.5, txt, ha="center", va="center",
                           fontsize=7,
                           color="white" if i >= 4 else "black")
            else:
                y_label = 1.05 + 0.30 * (i % 2)
                ax_bd.annotate(
                    txt,
                    xy=(s + dur / 2, 0.85),
                    xytext=(s + dur / 2, y_label),
                    ha="center", va="bottom", fontsize=7, color="black",
                    arrowprops=dict(arrowstyle="-", color="grey",
                                    linewidth=0.4, shrinkA=0, shrinkB=0),
                )
        ax_bd.set_xlim(bd_start - bd_total * 0.05, bd_end + bd_total * 0.05)
        ax_bd.set_ylim(0, 2.0)
        ax_bd.set_yticks([0.5])
        ax_bd.set_yticklabels(["Cilium bootstrap\n(zoomed)"], fontsize=9)
        ax_bd.set_xlabel("seconds since T1 (node registered) — zoomed view of the cilium-agent bootstrap window")
        ax_bd.set_title(
            f"Cilium agent internal breakdown — image-pull / cont start "
            f"{pre_bs_dur:.2f}s, then bootstrap phases (zoomed) total {bd_total:.2f}s",
            fontsize=9, loc="left",
        )
        ax_bd.grid(True, axis="x", alpha=0.3)

    p = out_dir / "phase_profile.png"
    fig.tight_layout()
    fig.savefig(p, dpi=140)
    plt.close(fig)
    return p


def _mean_offset_metric(ok: pd.DataFrame, col: str) -> float | None:
    """Mean of a numeric metric column (not a timestamp delta)."""
    if col not in ok.columns:
        return None
    s = pd.to_numeric(ok[col], errors="coerce").dropna()
    if s.empty:
        return None
    return float(s.mean())


def _backfill_cilium_deep(df: pd.DataFrame, run_dir: Path) -> None:
    """Populate the expanded cilium_bootstrap_*/regen_* columns from per-iteration
    `iter-NNN/cilium_deep_headline.json` payloads. Mutates `df` in place. No-op
    when no headline files are present (i.e. the run wasn't `--deep-cilium`).
    """
    import json as _json
    from .cilium_deep import HEADLINE_COLUMNS, headline_to_columns
    if df.empty or "iteration" not in df.columns:
        return
    needs_backfill = False
    for col in HEADLINE_COLUMNS:
        if col not in df.columns or pd.to_numeric(df[col], errors="coerce").isna().all() \
                if col.endswith("_s") else col not in df.columns:
            needs_backfill = True
            break
    if not needs_backfill:
        return
    for col in HEADLINE_COLUMNS:
        if col not in df.columns:
            df[col] = None
    for idx, row in df.iterrows():
        it = row.get("iteration")
        if pd.isna(it):
            continue
        p = run_dir / f"iter-{int(it):03d}" / "cilium_deep_headline.json"
        if not p.exists():
            continue
        try:
            headline = _json.loads(p.read_text())
        except Exception:
            continue
        cols = headline_to_columns(headline)
        for col, val in cols.items():
            if val is not None:
                df.at[idx, col] = val


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
