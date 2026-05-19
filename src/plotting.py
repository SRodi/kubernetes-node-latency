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
    # T1 -> T1c "kubelet blocked on CNI" is split into four explicit sub-lanes
    # on the main chart so the reader can see the dominant cost without
    # needing the zoom. The post-image-pull tail is decomposed further in
    # the zoomed subplot below the main chart.
    ("Scheduler latency",                    "T1_node_registered", "T_pod_scheduled",    "scheduler_wait"),
    ("Kubelet sandbox + image-pull init",    "T_pod_scheduled",    "T_image_pull_start", "sandbox"),
    ("Cilium agent image pull",              "T_image_pull_start", "T_image_pulled",     "image_pull"),
    ("Cilium init-container chain",
                                              "T_image_pulled",    "T1c_cni_conflist",   "cni"),
    ("Kubelet: residual status sync",        "T1c_cni_conflist",   "T4_node_ready",      "kubelet"),
    ("Cilium agent bootstrap",               "T2_cilium_started",  "T3_cilium_ready",    "cilium"),
    ("Scheduling block (cilium taint)",      "T4_node_ready",      "T4b_schedulable",    "scheduler"),
    # Trigger pod's CNI ADD + sandbox setup: from when the scheduler bound the
    # trigger pod to the new node, to when its first container reports Running.
    # This is the workload-side analogue of cilium IPAM and is what gates the
    # "node became useful" moment for actual workloads.
    ("Trigger pod sandbox / CNI ADD",        "T_trigger_scheduled", "T5_pod_running",    "trigger_pod"),
]

ACTOR_COLORS = {
    "cloud":           "#9aa0a6",  # grey
    "kubelet":         "#4285f4",  # blue
    "cni":             "#fb8c00",  # orange
    "cilium":          "#34a853",  # green
    "cilium_regen":    "#6087c5",  # mid-blue, lighter than darkest in regen zoom palette
    "image_pull":      "#fbbf24",  # amber, matches image-pull segment in CNI zoom
    "scheduler":       "#ea4335",  # red (post-Ready taint block)
    "scheduler_wait":  "#c7d2fe",  # lavender (T1->Ts pre-bind)
    "sandbox":         "#fde68a",  # pale amber (kubelet pre-pull setup)
    "kubelet_main":    "#93c5fd",  # light blue (kubelet starting agent main container)
    "trigger_pod":     "#a855f7",  # purple (trigger pod CNI ADD / sandbox setup)
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
    _backfill_taint_observed(df, iterations_csv.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    ok = _ok(df)
    if ok.empty:
        return paths

    # Derive a `(provider @ region)` title from the CSV when not supplied
    # (e.g. when `plot_all` is invoked via `src.cli plot <results_dir>` for
    # post-hoc regeneration). The cluster identifier is what the reader uses
    # to disambiguate side-by-side plots across providers.
    if not title:
        prov = ok["provider"].dropna().iloc[0] if "provider" in ok.columns and not ok["provider"].dropna().empty else None
        reg = ok["region"].dropna().iloc[0] if "region" in ok.columns and not ok["region"].dropna().empty else None
        if prov and reg:
            title = f"({prov} @ {reg})"
        elif prov:
            title = f"({prov})"

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

    # 3. stacked phase per iteration — split into two panels so the
    #    IaaS-side (T0 -> T1) variance doesn't dominate the K8s-networking
    #    bars visually. Top panel: VM provision + node registered (T0->T1).
    #    Bottom panel: K8s networking (T1 -> T5_pod_running), the part
    #    that's actually comparable across providers.
    iaas_dur = _seconds(ok.get("T1_node_registered"), ok.get("T0_pod_created")).clip(lower=0) \
        if ("T1_node_registered" in ok.columns and "T0_pod_created" in ok.columns) \
        else pd.Series(dtype=float)
    k8s_phases = pd.DataFrame()
    if {"T1_node_registered", "T1c_cni_conflist", "T4_node_ready",
        "T4b_schedulable"}.issubset(ok.columns):
        k8s_phases["T1 -> T1c (CNI conflist)"] = _seconds(
            ok["T1c_cni_conflist"], ok["T1_node_registered"]).clip(lower=0)
        k8s_phases["T1c -> T4 (kubelet ready)"] = _seconds(
            ok["T4_node_ready"], ok["T1c_cni_conflist"]).clip(lower=0)
        k8s_phases["T4 -> T4b (sched. block)"] = _seconds(
            ok["T4b_schedulable"], ok["T4_node_ready"]).clip(lower=0)
        if "T5_pod_running" in ok.columns:
            k8s_phases["T4b -> T5 (sandbox / CNI ADD)"] = _seconds(
                ok["T5_pod_running"], ok["T4b_schedulable"]).clip(lower=0)
    if not k8s_phases.empty or not iaas_dur.empty:
        fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(10, 7), sharex=True,
                                             gridspec_kw={"height_ratios": [1, 2]})
        x = np.arange(1, max(len(iaas_dur), len(k8s_phases)) + 1)
        if not iaas_dur.empty:
            ax_top.bar(x, iaas_dur.fillna(0).values, color=ACTOR_COLORS["cloud"],
                       label="T0 -> T1 (IaaS: autoscaler + VM + kubelet boot)")
            ax_top.set_ylabel("seconds")
            ax_top.set_title(
                f"IaaS-side: cloud autoscaler + VM provisioning {title}\n"
                "(high variance — not directly comparable across clouds)")
            ax_top.legend(loc="upper right", fontsize=8)
            ax_top.grid(True, axis="y", alpha=0.3)
        if not k8s_phases.empty:
            bottom = np.zeros(len(k8s_phases))
            x2 = np.arange(1, len(k8s_phases) + 1)
            palette = [ACTOR_COLORS["cni"], ACTOR_COLORS["kubelet"],
                       ACTOR_COLORS["scheduler"], ACTOR_COLORS["cilium"]]
            for i, col in enumerate(k8s_phases.columns):
                vals = k8s_phases[col].fillna(0).values
                ax_bot.bar(x2, vals, bottom=bottom, label=col,
                           color=palette[i % len(palette)])
                bottom += vals
            cilium = pd.to_numeric(ok.get("cilium_init_duration_s"), errors="coerce")
            if cilium is not None and cilium.notna().any():
                ax_bot.plot(x2, cilium.values, color="black", marker="D",
                            linewidth=1.2,
                            label="cilium agent init (T3 \u2212 T2, parallel)")
            ax_bot.set_xlabel("iteration"); ax_bot.set_ylabel("seconds")
            ax_bot.set_title(
                "K8s networking: T1 -> T5_pod_running "
                "(directly comparable across providers)")
            ax_bot.legend(loc="upper right", fontsize=8)
            ax_bot.grid(True, axis="y", alpha=0.3)
        p = out_dir / "phase_stacked.png"; fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig); paths.append(p)

    # 4. latency vs iteration — show BOTH the IaaS T0->T1 line and the
    #    K8s-networking T1->T5 line so the reader sees they're independent.
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = range(1, len(metrics_df) + 1)
    plotted = False
    if "node_register_latency_s" in metrics_df:
        ax.plot(x, metrics_df["node_register_latency_s"], marker="s",
                color=ACTOR_COLORS["cloud"], label="T0->T1 (IaaS)")
        plotted = True
    if "time_to_runnable_s" in metrics_df:
        ax.plot(x, metrics_df["time_to_runnable_s"], marker="o",
                color=ACTOR_COLORS["cilium"],
                label="T1->T5 time_to_runnable (K8s networking)")
        plotted = True
    if not plotted and "node_startup_latency_s" in metrics_df:
        ax.plot(x, metrics_df["node_startup_latency_s"], marker="o",
                label="T0->T4 (legacy)")
    ax.set_xlabel("iteration"); ax.set_ylabel("seconds")
    ax.set_title(f"Per-iteration latency {title}")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    p = out_dir / "latency_vs_iteration.png"; fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig); paths.append(p)

    # 5. CDF — prefer time_to_runnable_s (T1-anchored, IaaS-noise excluded).
    cdf_metric = "time_to_runnable_s" if "time_to_runnable_s" in metrics_df \
        and pd.to_numeric(metrics_df["time_to_runnable_s"], errors="coerce").dropna().size \
        else "node_startup_latency_s"
    s = metrics_df.get(cdf_metric)
    if s is not None and s.dropna().size:
        s = s.dropna().sort_values().reset_index(drop=True)
        cdf = (np.arange(1, len(s) + 1)) / len(s)
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.plot(s.values, cdf, marker=".")
        for q, label in [(0.50, "p50"), (0.90, "p90"), (0.99, "p99")]:
            v = float(s.quantile(q))
            ax.axvline(v, linestyle="--", alpha=0.4)
            ax.text(v, q, f" {label}={v:.1f}s", va="center")
        ax.set_xlabel(f"{cdf_metric} (s)"); ax.set_ylabel("CDF")
        ax.set_title(f"CDF of {cdf_metric} {title}")
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
        """Median offset (seconds) of `col` relative to T0, across iterations.

        Despite the historical name, this returns the **median** — median is
        robust to cold-pool tails (e.g. GKE Autopilot T1: 7s vs 170s in the
        same run); mean would visually distort the Gantt.
        """
        if col not in ok.columns:
            return None
        s = _seconds(ok[col], ok["T0_pod_created"])
        s = s[s.notna()]
        if s.empty:
            return None
        return float(s.median())

    lanes: list[tuple[str, float, float, str]] = []
    for label, start_col, end_col, actor in PROFILE_LANES:
        if actor == "cloud":
            continue  # rendered as a numeric annotation, not a bar
        s_off = _mean_offset(start_col)
        e_off = _mean_offset(end_col)
        if s_off is None or e_off is None:
            continue
        dur = max(e_off - s_off, 0.0)
        if dur <= 1e-3:
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

    # ---- Cilium endpoint regeneration (per-endpoint avg, post-T3) ----
    # These are *per-endpoint average* durations, not wall-clock — but they
    # represent the agent's per-endpoint regen budget that runs after T3.
    # We render them chained as a single lane in the main Gantt anchored at
    # T3, and as a zoomed subplot below (mirrors the bootstrap layout).
    regen_means: list[tuple[str, float]] = []
    for label, col in CILIUM_REGEN_PHASES:
        m = _mean_offset_metric(ok, col)
        if m is None or m < 5e-3:
            continue
        regen_means.append((label, m))
    regen_breakdown: list[tuple[str, float, float]] = []
    regen_total = 0.0
    if regen_means and t3_off is not None:
        regen_total = sum(m for _, m in regen_means)
        cursor = t3_off - t1_off
        for label, dur in regen_means:
            short = label.replace("regen.", "")
            regen_breakdown.append((short, cursor, cursor + dur))
            cursor += dur
        # Add a synthetic lane to the main Gantt summarising the whole
        # post-T3 regen budget. `lanes` has already been rebased to T1, so
        # we use T1-relative offsets here too.
        lanes.append((
            "Cilium endpoint regeneration (per-endpoint avg, post-T3)",
            t3_off - t1_off, t3_off - t1_off + regen_total, "cilium_regen",
        ))

    all_lanes = lanes

    # Pre-compute init-container offsets (T1-relative) once — used by both
    # the "agent main container startup" main-chart lane below and the
    # init-chain zoom subplot further down.
    if "T1_node_registered" in ok.columns:
        ic_offsets = _mean_init_container_offsets(ok, ok["T1_node_registered"])
    else:
        ic_offsets = []
    last_init_end_t1 = max((e for _n, _s, e in ic_offsets), default=None)

    # Anchor the chain end to max(last init finished_at, T1c). Kubelet init
    # container `finished_at` is 1s-resolution, and the last init (e.g.
    # install-cni-binaries on upstream Cilium) is the one that writes
    # /etc/cni/net.d/*.conflist — our T1c watcher typically observes the
    # conflist a couple of seconds AFTER the kubelet's rounded
    # finished_at, so the last init is logically still running until T1c.
    # Snapping the chain end to T1c (when present and later) closes that
    # visual gap on the main chart and the zoom alike.
    t1c_off = _mean_offset("T1c_cni_conflist")
    t1c_rel = (t1c_off - t1_off) if t1c_off is not None else None
    if last_init_end_t1 is not None and t1c_rel is not None and t1c_rel > last_init_end_t1:
        last_init_end_t1 = t1c_rel

    # Make the main-chart "Cilium init-container chain" lane match the zoom
    # exactly: extend it from its PROFILE_LANES default (Tip -> T1c) to
    # Tip -> last_init_end. On managed-Cilium variants the conflist is
    # pre-baked, so T1c fires mid-chain — using T1c as the lane end would
    # hide most of the chain on the top chart.
    if last_init_end_t1 is not None:
        for i, (lbl, s_off, e_off, actor) in enumerate(all_lanes):
            if actor == "cni":
                if last_init_end_t1 > e_off:
                    all_lanes[i] = (lbl, s_off, last_init_end_t1, actor)
                break

    # ---- Synthetic main-chart lane: "Agent main container startup" ----
    # Spans the gap between the last init container finishing and the Cilium
    # agent main container reporting T2. On managed-Cilium variants where
    # T1c fires mid-chain (conflist pre-baked into the node image), this
    # gap is the most accurate visual representation of the kubelet ->
    # agent handover and is what the user sees as "why is T2 so much later
    # than T1c?". Rendered as a thin lane just before the agent bootstrap.
    if last_init_end_t1 is not None and t2_off is not None:
        main_start = last_init_end_t1
        main_end = t2_off - t1_off
        if main_end - main_start > 5e-3:
            # Insert before the Cilium agent bootstrap lane so the visual
            # ordering stays last-init -> main-startup -> bootstrap.
            insert_at = len(all_lanes)
            for i, (lbl, _s, _e, _a) in enumerate(all_lanes):
                if lbl == "Cilium agent bootstrap":
                    insert_at = i
                    break
            all_lanes.insert(insert_at, (
                "Agent main container startup",
                main_start, main_end, "kubelet_main",
            ))

    # ---- "Cilium init-container chain" zoom: Tip -> end of last init ----
    # The pre-image-pull portion (scheduler latency, sandbox prep, image pull)
    # is explicit on the main chart, so the zoom focuses on the per-init-
    # container chain. We extend the right edge to the LAST init container's
    # end (not just T1c) because managed-Cilium variants (e.g. AKS Azure CNI
    # Powered by Cilium) ship /etc/cni/net.d/05-cilium.conflist pre-baked in
    # the node image: T1c fires while most init containers are still waiting
    # to run, and the bulk of the chain lives in the T1c->T2 gap. T1c itself
    # is rendered inside the zoom as a dotted marker so it remains visible.
    # The kubelet->main-container handover (last_init.end -> T2) is left to
    # the main chart's T2 marker so the init zoom stays focused on the
    # init-container chain only.
    #
    # Kubelet init-container timestamps are 1s-resolution, so the naive
    # `finished_at - started_at` of each container often rounds to 0. We
    # instead derive durations from consecutive `started_at` deltas — i.e.
    # each container's duration is the time until the NEXT one starts (or,
    # for the last one, its own `finished_at - started_at`). This recovers
    # realistic per-container costs.
    cni_breakdown: list[tuple[str, float, float, str]] = []  # (label, start, end, color)
    img_pulled_off = _mean_offset("T_image_pulled")
    if ic_offsets and img_pulled_off is not None:
        zoom_start = img_pulled_off - t1_off
        # ic_offsets are already T1-relative; the zoom axis is also
        # T1-relative, so use the value directly.
        last_init_end = last_init_end_t1 if last_init_end_t1 is not None else zoom_start
        if last_init_end - zoom_start > 5e-3:
            palette_ic = ["#fed7aa", "#fdba74", "#fb923c", "#f97316",
                          "#ea580c", "#c2410c", "#9a3412", "#7c2d12"]

            def _push(label: str, start: float, end: float, color: str) -> None:
                if end - start > 5e-3:
                    cni_breakdown.append((label, start, end, color))

            # Use successive start offsets (clipped to the window) to compute
            # realistic per-init durations. The last init keeps its own
            # finished_at (the zoom's right edge).
            chain: list[tuple[str, float, float]] = []
            ic_in_window = [(n, s, e) for n, s, e in ic_offsets
                            if s < last_init_end and e > zoom_start]
            if ic_in_window:
                starts = [max(s, zoom_start) for _n, s, _e in ic_in_window]
                names = [n for n, _s, _e in ic_in_window]
                fa_ends = [e for _n, _s, e in ic_in_window]
                for i, name in enumerate(names):
                    s = starts[i]
                    if i + 1 < len(starts):
                        e = starts[i + 1]
                    else:
                        # Last init: extend through last_init_end (which is
                        # snapped to T1c when T1c > rounded finished_at, so
                        # the bar visually closes the gap to the conflist
                        # appearing on disk).
                        e = max(fa_ends[i], last_init_end)
                    e = min(e, last_init_end)
                    chain.append((name, s, e))

            cursor = zoom_start
            if chain:
                first_init_start = chain[0][1]
                if first_init_start > cursor:
                    _push("runc / init-chain spin-up",
                          cursor, first_init_start, "#fed7aa")
                    cursor = first_init_start
                for i, (name, s, e) in enumerate(chain):
                    _push(f"init: {name}", s, e,
                          palette_ic[i % len(palette_ic)])
                    cursor = max(cursor, e)

    # Markers on the main (top) chart: high-level milestones only.
    # Fine-grained sub-events (Tt, Ts, Tips, Tip, Tcsi, T1c) live entirely
    # inside the T1->T1c window and are rendered on the CNI zoom subplot.
    MAIN_MARKERS = [
        ("T1_node_registered", "T1"),
        ("T_pod_scheduled", "Ts"),
        ("T_image_pull_start", "Tips"),
        ("T_image_pulled", "Tip"),
        ("T1c_cni_conflist", "T1c"),
        ("T2_cilium_started", "T2"),
        ("T3_cilium_ready", "T3"),
        ("T4_node_ready", "T4"),
        ("T4b_schedulable", "T4b"),
        ("T_trigger_scheduled", "Tts"),
        ("T5_pod_running", "T5"),
    ]
    ZOOM_MARKERS = [
        ("T_csinode_ready", "Tcsi"),
        ("T_taint_observed", "Tt"),
    ]
    markers: list[tuple[str, float]] = []
    t4_off = _mean_offset("T4_node_ready")
    t4b_off = _mean_offset("T4b_schedulable")
    t4_t4b_coincide = (
        t4_off is not None and t4b_off is not None
        and abs(t4_off - t4b_off) <= 1e-3
    )
    for col, glyph in MAIN_MARKERS:
        off = _mean_offset(col)
        if off is None:
            continue
        # When T4 and T4b are the same instant (e.g. GKE without a cilium
        # taint), drop the plain T4 marker so the prominent T4b line below
        # is the only one drawn at that x.
        if glyph == "T4" and t4_t4b_coincide:
            continue
        markers.append((glyph, off - t1_off))

    # Layout: main wall-clock Gantt on top, plus optional zoomed subplots
    # (CNI add\u2192Ready decomposition, Cilium-internal bootstrap).
    has_cilium_bd = bool(cilium_breakdown)
    has_cni_bd = bool(cni_breakdown)
    has_regen_bd = bool(regen_breakdown)
    n_main = len(all_lanes)
    sub_count = (1 if has_cni_bd else 0) + (1 if has_cilium_bd else 0) + (1 if has_regen_bd else 0)
    fig_height = 4 + 0.35 * n_main + 2.0 * sub_count
    if sub_count:
        ratios = [max(n_main, 4)] + [2.0] * sub_count
        fig, axes = plt.subplots(
            1 + sub_count, 1, figsize=(12, fig_height),
            gridspec_kw={"height_ratios": ratios},
        )
        ax = axes[0]
        sub_axes = list(axes[1:])
    else:
        fig, ax = plt.subplots(figsize=(12, fig_height))
        sub_axes = []
    ax_cni_bd = sub_axes.pop(0) if has_cni_bd else None
    ax_bd = sub_axes.pop(0) if has_cilium_bd else None
    ax_regen = sub_axes.pop(0) if has_regen_bd else None

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
        # Render T4b prominently: it marks when the node becomes schedulable
        # (cilium taint removed) and is the primary user-facing readiness
        # outcome of the run.
        if glyph == "T4b":
            ax.axvline(off, color=ACTOR_COLORS["scheduler"], linestyle="-",
                       alpha=0.85, linewidth=1.6)
            label = "T4 = T4b\n(pod schedulable)" if t4_t4b_coincide else "T4b\n(pod schedulable)"
            # Place outside the chart, just above the top spine.
            ax.annotate(
                label,
                xy=(off, 1.0), xycoords=("data", "axes fraction"),
                xytext=(0, 4), textcoords="offset points",
                ha="center", va="bottom", fontsize=8,
                color=ACTOR_COLORS["scheduler"], fontweight="bold",
                annotation_clip=False,
            )
        elif glyph == "T5":
            # T5 is the workload-side "node became useful" moment — trigger
            # pod's first container Running. Render as prominently as T4b but
            # in trigger_pod purple so the two milestones (scheduler-ready vs
            # workload-running) are visually distinct.
            ax.axvline(off, color=ACTOR_COLORS["trigger_pod"], linestyle="-",
                       alpha=0.85, linewidth=1.6)
            ax.annotate(
                "T5\n(pod running)",
                xy=(off, 1.0), xycoords=("data", "axes fraction"),
                xytext=(0, 4), textcoords="offset points",
                ha="center", va="bottom", fontsize=8,
                color=ACTOR_COLORS["trigger_pod"], fontweight="bold",
                annotation_clip=False,
            )
        elif glyph == "Tts":
            # Trigger-pod scheduled: subtle dotted line in trigger_pod colour
            # so the reader can see where the T4b -> T5 sandbox window opens.
            ax.axvline(off, color=ACTOR_COLORS["trigger_pod"], linestyle=":",
                       alpha=0.6, linewidth=1.0)
        elif glyph == "T1c":
            # T1c (conflist written / discovered on disk) is a key transition
            # marker: kubelet stops reporting "no CNI" after this point.
            # Render it as a colored dashed line so the reader can see how
            # it relates to the init-container chain on the lane below
            # (especially on managed-Cilium variants where T1c falls
            # mid-chain because the conflist is pre-baked into the image).
            ax.axvline(off, color="#b91c1c", linestyle="--", alpha=0.85,
                       linewidth=1.2)
            ax.text(off, n_main + 0.6, glyph, ha="center", va="bottom",
                    fontsize=8, color="#b91c1c", fontweight="bold")
        else:
            ax.axvline(off, color="black", linestyle=":", alpha=0.35, linewidth=0.8)
            ax.text(off, n_main + 0.6, glyph, ha="center", va="bottom",
                    fontsize=8, color="black")

    ax.set_yticks(y_positions)
    ax.set_yticklabels([label for label, _, _, _ in all_lanes], fontsize=9)
    ax.set_xlabel("seconds since T1 (node registered)")
    ax.set_ylim(0.2, n_main + 1.2)

    # Emphasise the T0->T1 cloud bringup latency as a prominent suptitle so
    # the reader immediately sees the dominant (and not-to-scale) cost.
    fig.suptitle(
        f"Cloud / autoscaler + VM bringup  T0\u2192T1 = {cloud_dur:.2f}s  (not shown on x-axis)",
        fontsize=13, fontweight="bold", color="#111111", y=0.995,
    )
    title_lines = [
        f"Phase profile {title}",
        "bars overlapping on x = parallel; back-to-back = sequential.",
    ]
    ax.set_title("\n".join(title_lines), fontsize=9, loc="center", pad=22)
    ax.grid(True, axis="x", alpha=0.3)

    from matplotlib.patches import Patch
    legend_handles = [
        Patch(facecolor=ACTOR_COLORS[a], edgecolor="black", label=a)
        for a in used_actors
    ]
    ax.legend(handles=legend_handles, loc="lower left", fontsize=8, title="actor")

    # ---- T1\u2192T1c install-cni decomposition (zoomed) ----
    if has_cni_bd and ax_cni_bd is not None:
        bd_start = min(s for _, s, _, _ in cni_breakdown)
        bd_end = max(e for _, _, e, _ in cni_breakdown)
        bd_total = max(bd_end - bd_start, 1e-3)
        for i, (label, s, e, color) in enumerate(cni_breakdown):
            dur = e - s
            ax_cni_bd.barh(0.5, dur, left=s, height=0.7, color=color,
                           edgecolor="black", linewidth=0.4)
            rel = dur / bd_total if bd_total > 0 else 0
            inline = rel > 0.18
            txt = f"{label}\n{dur*1000:.0f}ms" if dur < 0.5 else f"{label}\n{dur:.2f}s"
            if inline:
                ax_cni_bd.text(s + dur / 2, 0.5, txt, ha="center", va="center",
                               fontsize=7, color="black")
            else:
                y_label = 1.05 + 0.30 * (i % 2)
                ax_cni_bd.annotate(
                    txt,
                    xy=(s + dur / 2, 0.85),
                    xytext=(s + dur / 2, y_label),
                    ha="center", va="bottom", fontsize=7, color="black",
                    arrowprops=dict(arrowstyle="-", color="grey",
                                    linewidth=0.4, shrinkA=0, shrinkB=0),
                )
        # Mark fine-grained sub-events inside the zoom (replaces the markers
        # that used to clutter the main chart).
        for col, glyph in ZOOM_MARKERS:
            off = _mean_offset(col)
            if off is None:
                continue
            x = off - t1_off
            if not (bd_start <= x <= bd_end):
                continue
            ax_cni_bd.axvline(x, color="black", linestyle=":", alpha=0.35, linewidth=0.8)
            ax_cni_bd.text(x, 1.95, glyph, ha="center", va="top",
                           fontsize=8, color="black")
        # Mark T1c (conflist on disk) inside the zoom. Vital context now
        # that the zoom extends past T1c on managed-Cilium variants where
        # the conflist is pre-baked into the node image and T1c fires
        # before most of the init chain runs.
        t1c_off = _mean_offset("T1c_cni_conflist")
        if t1c_off is not None:
            x = t1c_off - t1_off
            if bd_start <= x <= bd_end:
                ax_cni_bd.axvline(x, color="#b91c1c", linestyle="--", linewidth=1.2)
                ax_cni_bd.text(x, 1.7, "T1c (conflist)", color="#b91c1c",
                               fontsize=7, ha="center", va="top")
        # Mark T_csinode_ready inside the zoom if available
        t_csi_off = _mean_offset("T_csinode_ready")
        if t_csi_off is not None:
            x = t_csi_off - t1_off
            if bd_start <= x <= bd_end:
                ax_cni_bd.axvline(x, color="#1d4ed8", linestyle="--", linewidth=1.2)
                ax_cni_bd.text(x, 1.7, "CSINode ready", color="#1d4ed8",
                               fontsize=7, ha="center", va="top")
        ax_cni_bd.set_xlim(bd_start - bd_total * 0.05, bd_end + bd_total * 0.05)
        ax_cni_bd.set_ylim(0, 2.0)
        ax_cni_bd.set_yticks([0.5])
        ax_cni_bd.set_yticklabels(["init-container\nchain"], fontsize=8)
        ax_cni_bd.set_xlabel(
            "seconds since T1 (node registered) \u2014 zoom of the Cilium init-container chain"
        )
        ax_cni_bd.set_title(
            f"{title + '  ' if title else ''}"
            f"Cilium init-container chain ({bd_total:.2f}s, Tip\u2192last init end): "
            f"per-init durations derived from consecutive start offsets",
            fontsize=9, loc="left",
        )
        ax_cni_bd.grid(True, axis="x", alpha=0.3)

    # ---- Cilium internal breakdown (zoomed) ----
    if has_cilium_bd and ax_bd is not None:
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
        ax_bd.set_yticklabels(["Cilium agent\nbootstrap"], fontsize=9)
        ax_bd.set_xlabel("seconds since T1 (node registered) — zoomed view of the cilium-agent bootstrap window")
        ax_bd.set_title(
            f"{title + '  ' if title else ''}"
            f"Cilium agent bootstrap \u2014 container-start \u2192 bootstrap gap "
            f"{pre_bs_dur:.2f}s, then bootstrap phases (zoomed) total {bd_total:.2f}s",
            fontsize=9, loc="left",
        )
        ax_bd.grid(True, axis="x", alpha=0.3)

    # ---- Cilium endpoint regeneration (zoomed, per-endpoint avg) ----
    if has_regen_bd and ax_regen is not None:
        palette = ["#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb",
                   "#1d4ed8", "#1e40af", "#1e3a8a"]
        bd_start = regen_breakdown[0][1]
        bd_end = regen_breakdown[-1][2]
        bd_total = bd_end - bd_start
        for i, (label, s, e) in enumerate(regen_breakdown):
            dur = e - s
            color = palette[i % len(palette)]
            ax_regen.barh(0.5, dur, left=s, height=0.7, color=color,
                          edgecolor="black", linewidth=0.4)
            rel = dur / bd_total if bd_total > 0 else 0
            inline = rel > 0.20
            txt = f"{label}\n{dur*1000:.0f}ms" if dur < 0.5 else f"{label}\n{dur:.2f}s"
            if inline:
                ax_regen.text(s + dur / 2, 0.5, txt, ha="center", va="center",
                              fontsize=7,
                              color="white" if i >= 4 else "black")
            else:
                y_label = 1.05 + 0.30 * (i % 2)
                ax_regen.annotate(
                    txt,
                    xy=(s + dur / 2, 0.85),
                    xytext=(s + dur / 2, y_label),
                    ha="center", va="bottom", fontsize=7, color="black",
                    arrowprops=dict(arrowstyle="-", color="grey",
                                    linewidth=0.4, shrinkA=0, shrinkB=0),
                )
        ax_regen.set_xlim(bd_start - bd_total * 0.05, bd_end + bd_total * 0.05)
        ax_regen.set_ylim(0, 2.0)
        ax_regen.set_yticks([0.5])
        ax_regen.set_yticklabels(["Cilium endpoint\nregeneration"], fontsize=8)
        ax_regen.set_xlabel("seconds since T1 (node registered) \u2014 per-endpoint avg, anchored at T3")
        ax_regen.set_title(
            f"{title + '  ' if title else ''}"
            f"Cilium endpoint regeneration (per-endpoint avg, post-T3) \u2014 total {bd_total:.2f}s",
            fontsize=9, loc="left",
        )
        ax_regen.grid(True, axis="x", alpha=0.3)

    p = out_dir / "phase_profile.png"
    fig.tight_layout()
    fig.savefig(p, dpi=140)
    plt.close(fig)
    return p


def _mean_offset_metric(ok: pd.DataFrame, col: str) -> float | None:
    """Median of a numeric metric column (not a timestamp delta).

    Name kept for backward compat; implementation is median, matching the
    phase_profile/Gantt mean->median switch for cold-tail robustness.
    """
    if col not in ok.columns:
        return None
    s = pd.to_numeric(ok[col], errors="coerce").dropna()
    if s.empty:
        return None
    return float(s.median())


def _mean_init_container_durations(ok: pd.DataFrame) -> list[tuple[str, float]]:
    """Parse init_containers_json (per iteration) and return median durations
    per init-container name, ordered by the canonical first-seen ordering.

    Each entry is (name, median_duration_seconds). Containers with < 5 ms median
    are filtered out — they'd be invisible in the plot.
    """
    rows = _init_container_windows(ok)
    durations: dict[str, list[float]] = {}
    order: list[str] = []
    for name, _start, _end, dur in rows:
        if name not in durations:
            durations[name] = []
            order.append(name)
        durations[name].append(dur)
    out: list[tuple[str, float]] = []
    for name in order:
        vals = durations[name]
        if not vals:
            continue
        median = float(pd.Series(vals).median())
        if median >= 5e-3:
            out.append((name, median))
    return out


def _mean_init_container_offsets(ok: pd.DataFrame, t1_off_per_row: pd.Series) -> list[tuple[str, float, float]]:
    """Return per-init-container (name, median_start_offset_s, median_end_offset_s)
    relative to T1, using absolute timestamps in init_containers_json.

    Despite the historical name, aggregates with **median** for cold-tail
    robustness (consistent with the phase_profile main-marker offsets).

    `t1_off_per_row` must be aligned with `ok` and contain each iteration's
    T1 absolute time as a pandas datetime; rows where T1 is missing are
    skipped.
    """
    if "init_containers_json" not in ok.columns:
        return []
    import json as _json
    from datetime import datetime as _dt
    starts: dict[str, list[float]] = {}
    ends: dict[str, list[float]] = {}
    order: list[str] = []
    t1_series = pd.to_datetime(t1_off_per_row, utc=True, errors="coerce")
    for raw, t1 in zip(ok["init_containers_json"], t1_series):
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        if pd.isna(t1):
            continue
        try:
            entries = _json.loads(raw)
        except Exception:
            continue
        for entry in entries:
            name = entry.get("name")
            sa = entry.get("started_at")
            fa = entry.get("finished_at")
            if not (name and sa and fa):
                continue
            try:
                sa_t = _dt.fromisoformat(sa.replace("Z", "+00:00"))
                fa_t = _dt.fromisoformat(fa.replace("Z", "+00:00"))
            except Exception:
                continue
            s_off = (sa_t - t1.to_pydatetime()).total_seconds()
            e_off = (fa_t - t1.to_pydatetime()).total_seconds()
            if name not in starts:
                starts[name] = []
                ends[name] = []
                order.append(name)
            starts[name].append(s_off)
            ends[name].append(e_off)
    out: list[tuple[str, float, float]] = []
    for name in order:
        s_med = float(pd.Series(starts[name]).median())
        e_med = float(pd.Series(ends[name]).median())
        # Keep all init containers even if their (finished_at - started_at)
        # rounds to 0 — the zoom recomputes per-init durations from
        # consecutive start offsets, which is more accurate than the
        # 1s-resolution finished_at.
        out.append((name, s_med, e_med))
    out.sort(key=lambda x: x[1])
    return out


def _init_container_windows(ok: pd.DataFrame) -> list[tuple[str, float, float, float]]:
    """Internal helper: yield (name, start_epoch, end_epoch, duration) for
    every init-container row across iterations. Used by both duration and
    offset helpers.
    """
    if "init_containers_json" not in ok.columns:
        return []
    import json as _json
    from datetime import datetime as _dt
    rows: list[tuple[str, float, float, float]] = []
    for raw in ok["init_containers_json"].dropna():
        try:
            entries = _json.loads(raw)
        except Exception:
            continue
        for entry in entries:
            name = entry.get("name")
            sa = entry.get("started_at")
            fa = entry.get("finished_at")
            if not (name and sa and fa):
                continue
            try:
                sa_t = _dt.fromisoformat(sa.replace("Z", "+00:00"))
                fa_t = _dt.fromisoformat(fa.replace("Z", "+00:00"))
            except Exception:
                continue
            dur = max((fa_t - sa_t).total_seconds(), 0.0)
            rows.append((name, sa_t.timestamp(), fa_t.timestamp(), dur))
    return rows


def _backfill_taint_observed(df: pd.DataFrame, run_dir: Path) -> None:
    """Populate T_taint_observed + taint_observed_offset_s for historical runs
    by scanning raw_events.jsonl for the first `node_blocking_taint_present`
    event per iteration. Mutates `df` in place. No-op when columns already
    have values (i.e. run was captured with the new collector) or events
    file is missing.
    """
    import json as _json
    from datetime import datetime as _dt
    if df.empty or "iteration" not in df.columns:
        return
    if "T_taint_observed" in df.columns and df["T_taint_observed"].notna().any():
        return
    events_path = run_dir / "raw_events.jsonl"
    if not events_path.exists():
        return
    first_taint: dict[int, str] = {}
    current_iter: int | None = None
    try:
        with events_path.open() as f:
            for line in f:
                try:
                    ev = _json.loads(line)
                except Exception:
                    continue
                kind = ev.get("kind")
                if kind == "iteration_start":
                    current_iter = int(ev.get("iteration", 0)) or None
                elif kind == "node_blocking_taint_present" and current_iter is not None:
                    if current_iter not in first_taint:
                        first_taint[current_iter] = ev.get("ts")
                elif kind == "iteration_end":
                    current_iter = None
    except Exception:
        return
    if not first_taint:
        return
    if "T_taint_observed" not in df.columns:
        df["T_taint_observed"] = None
    if "taint_observed_offset_s" not in df.columns:
        df["taint_observed_offset_s"] = None
    for idx, row in df.iterrows():
        it = row.get("iteration")
        if pd.isna(it):
            continue
        ts = first_taint.get(int(it))
        if not ts:
            continue
        df.at[idx, "T_taint_observed"] = ts
        t1 = row.get("T1_node_registered")
        if isinstance(t1, str) and t1:
            try:
                t1_dt = _dt.fromisoformat(t1.replace("Z", "+00:00"))
                taint_dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                df.at[idx, "taint_observed_offset_s"] = (taint_dt - t1_dt).total_seconds()
            except Exception:
                pass


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


def _run_label(csv_path: Path) -> str:
    """Resolve a readable provider/region label from a run dir."""
    meta_p = csv_path.parent / "run_metadata.json"
    if meta_p.exists():
        try:
            import json
            m = json.loads(meta_p.read_text())
            provider = (m.get("config") or {}).get("provider")
            region = (m.get("cluster") or {}).get("region")
            argv = m.get("cli_argv") or []
            extras = []
            if "--aks-node-provisioning" in argv:
                try:
                    extras.append(argv[argv.index("--aks-node-provisioning") + 1])
                except IndexError:
                    pass
            if provider:
                parts = [provider]
                if region:
                    parts.append(region)
                if extras:
                    parts.append("/".join(extras))
                return " · ".join(parts)
        except Exception:
            pass
    return csv_path.parent.name


def _plot_compare_phase_decomposition(csvs: list[Path], out_dir: Path) -> Path | None:
    """Cross-provider stacked-bar decomposition of the post-T1 K8s networking
    budget, with a Cilium-bootstrap drill-down and a known-suspects strip.

    Each row in the top panel is one run (one provider). Segments left-to-right
    show p50 seconds spent in each phase between T1 and T5_pod_running.
    """
    runs: list[tuple[str, pd.DataFrame]] = []
    for csv in csvs:
        try:
            df = _ok(pd.read_csv(csv))
        except Exception:
            continue
        if df.empty:
            continue
        runs.append((_run_label(csv), df))
    if not runs:
        return None

    def _p(df: pd.DataFrame, col: str, q: float) -> float:
        if col not in df.columns:
            return float("nan")
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        return float(s.quantile(q)) if not s.empty else float("nan")

    def _delta_p50(df: pd.DataFrame, end: str, start: str) -> float:
        if end not in df.columns or start not in df.columns:
            return float("nan")
        e = pd.to_datetime(df[end], errors="coerce", utc=True)
        s = pd.to_datetime(df[start], errors="coerce", utc=True)
        d = (e - s).dt.total_seconds().clip(lower=0).dropna()
        return float(d.median()) if not d.empty else float("nan")

    # ---- Panel A data: phase decomposition ----
    PHASES = [
        ("T1->T1c CNI conflist",        "T1c_cni_conflist",    "T1_node_registered",  ACTOR_COLORS["cni"]),
        ("T1c->T2 kubelet -> agent",    "T2_cilium_started",   "T1c_cni_conflist",    ACTOR_COLORS["kubelet_main"]),
        ("T2->T3 cilium bootstrap",     "T3_cilium_ready",     "T2_cilium_started",   ACTOR_COLORS["cilium"]),
        ("T3->T4 kubelet residual",     "T4_node_ready",       "T3_cilium_ready",     ACTOR_COLORS["kubelet"]),
        ("T4->T4b sched. block (taint)","T4b_schedulable",     "T4_node_ready",       ACTOR_COLORS["scheduler"]),
        ("T4b->T5 sandbox / CNI ADD",   "T5_pod_running",      "T4b_schedulable",     ACTOR_COLORS["trigger_pod"]),
    ]
    rows_a: list[tuple[str, list[float], float, float, float]] = []
    for label, df in runs:
        seg = [max(_delta_p50(df, end, start), 0.0) for _name, end, start, _c in PHASES]
        ttr_p50 = _p(df, "time_to_runnable_s", 0.5)
        ttr_p25 = _p(df, "time_to_runnable_s", 0.25)
        ttr_p75 = _p(df, "time_to_runnable_s", 0.75)
        if any(not np.isfinite(v) for v in seg) or not np.isfinite(ttr_p50):
            # Fallback: synthesise total from segments if time_to_runnable_s is missing
            ttr_p50 = float(np.nansum(seg)) if any(np.isfinite(v) for v in seg) else float("nan")
            ttr_p25 = ttr_p50
            ttr_p75 = ttr_p50
        rows_a.append((label, [v if np.isfinite(v) else 0.0 for v in seg],
                       ttr_p50, ttr_p25, ttr_p75))
    rows_a.sort(key=lambda r: r[2])  # ascending by p50 — fastest first at top

    # ---- Panel B data: Cilium bootstrap sub-phases ----
    rows_b: list[tuple[str, list[float]]] = []
    for label, df in runs:
        seg = [max(_p(df, col, 0.5), 0.0) if np.isfinite(_p(df, col, 0.5)) else 0.0
               for _l, col in CILIUM_BOOTSTRAP_PHASES]
        if sum(seg) > 0:
            rows_b.append((label, seg))

    # ---- Panel C data: known suspects ----
    SUSPECTS = [
        ("cilium-agent image pull (s)",    "#fbbf24"),
        ("CNI conflist install (s)",       "#fb8c00"),  # synth: T1c-T1
        ("Agent main container startup (s)", ACTOR_COLORS["kubelet_main"]),  # synth: T2 - last_init.finished_at
        ("Sched. block taint (s)",         ACTOR_COLORS["scheduler"]),
    ]

    def _agent_main_startup_p50(df: pd.DataFrame) -> float:
        """Median over iterations of (T2_cilium_started - max(init.finished_at)).
        Mirrors the synthetic 'Agent main container startup' lane in phase_profile.
        """
        if "init_containers_json" not in df.columns or "T2_cilium_started" not in df.columns:
            return float("nan")
        import json as _json
        from datetime import datetime as _dt
        deltas: list[float] = []
        t2_series = pd.to_datetime(df["T2_cilium_started"], errors="coerce", utc=True)
        for raw, t2 in zip(df["init_containers_json"], t2_series):
            if raw is None or (isinstance(raw, float) and pd.isna(raw)) or pd.isna(t2):
                continue
            try:
                entries = _json.loads(raw)
            except Exception:
                continue
            last_end = None
            for ic in entries:
                fa = ic.get("finished_at")
                if not fa:
                    continue
                try:
                    fa_dt = _dt.fromisoformat(fa.replace("Z", "+00:00"))
                except Exception:
                    continue
                if last_end is None or fa_dt > last_end:
                    last_end = fa_dt
            if last_end is None:
                continue
            delta = (t2.to_pydatetime() - last_end).total_seconds()
            if delta >= 0:
                deltas.append(delta)
        if not deltas:
            return float("nan")
        return float(pd.Series(deltas).median())

    rows_c: list[tuple[str, list[float]]] = []
    for label, df in runs:
        vals = [
            _p(df, "image_pull_s", 0.5),
            _delta_p50(df, "T1c_cni_conflist", "T1_node_registered"),
            _agent_main_startup_p50(df),
            _p(df, "cilium_scheduling_block_s", 0.5),
        ]
        rows_c.append((label, [v if np.isfinite(v) else 0.0 for v in vals]))

    # ---- Figure layout ----
    n = len(rows_a)
    h_a = max(2.2, 0.45 * n + 1.5)
    h_b = max(2.0, 0.40 * len(rows_b) + 1.2)
    h_c = max(2.0, 0.50 * len(rows_c) + 1.0)
    fig, axes = plt.subplots(
        3, 1, figsize=(16, h_a + h_b + h_c),
        gridspec_kw={"height_ratios": [h_a, h_b, h_c]},
    )
    ax_a, ax_b, ax_c = axes

    # --- Panel A: stacked phase decomposition ---
    y = np.arange(n)
    cursor = np.zeros(n)
    for i, (phase_label, _end, _start, color) in enumerate(PHASES):
        widths = np.array([r[1][i] for r in rows_a])
        ax_a.barh(y, widths, left=cursor, color=color, edgecolor="white",
                  linewidth=0.6, label=phase_label)
        for j, w in enumerate(widths):
            if w >= 1.0:
                ax_a.text(cursor[j] + w / 2, y[j], f"{w:.1f}",
                          ha="center", va="center", fontsize=8,
                          color="white", fontweight="bold")
        cursor += widths
    # IQR whisker on total
    for j, (_lab, _seg, p50, p25, p75) in enumerate(rows_a):
        ax_a.errorbar(p50, y[j], xerr=[[max(p50 - p25, 0)], [max(p75 - p50, 0)]],
                      fmt="none", ecolor="#333", capsize=3, linewidth=1.0)
        ax_a.text(cursor[j] + 0.6, y[j],
                  f"p50 {p50:.1f}s  (p25 {p25:.1f}  p75 {p75:.1f})",
                  va="center", fontsize=8, color="#333")
    ax_a.set_yticks(y); ax_a.set_yticklabels([r[0] for r in rows_a], fontsize=9)
    ax_a.invert_yaxis()
    ax_a.set_xlabel("seconds since T1 (node registered)  —  p50 across iterations")
    ax_a.set_title(
        "K8s networking phase decomposition — provider comparison (p50)",
        fontsize=11, fontweight="bold",
    )
    ax_a.grid(True, axis="x", alpha=0.3)
    ax_a.legend(loc="center left", bbox_to_anchor=(1.02, 0.5),
                fontsize=8, framealpha=0.95, title="phase")

    # --- Panel B: Cilium bootstrap sub-phases ---
    if rows_b:
        yb = np.arange(len(rows_b))
        cursor_b = np.zeros(len(rows_b))
        palette_b = [
            "#a7f3d0", "#6ee7b7", "#34d399", "#fde68a", "#10b981",
            "#059669", "#047857", "#065f46", "#bef264", "#a3e635", "#84cc16",
        ]
        for i, (lbl, _col) in enumerate(CILIUM_BOOTSTRAP_PHASES):
            widths = np.array([r[1][i] for r in rows_b])
            if widths.sum() <= 0:
                continue
            color = palette_b[i % len(palette_b)]
            ax_b.barh(yb, widths, left=cursor_b, color=color,
                      edgecolor="white", linewidth=0.5,
                      label=lbl.replace("bootstrap.", ""))
            for j, w in enumerate(widths):
                if w >= 0.5:
                    ax_b.text(cursor_b[j] + w / 2, yb[j], f"{w:.1f}",
                              ha="center", va="center", fontsize=7, color="#111")
            cursor_b += widths
        for j, (_lab, seg) in enumerate(rows_b):
            tot = sum(seg)
            ax_b.text(tot + 0.2, yb[j], f"{tot:.1f}s",
                      va="center", fontsize=8, color="#333")
        ax_b.set_yticks(yb); ax_b.set_yticklabels([r[0] for r in rows_b], fontsize=9)
        ax_b.invert_yaxis()
        ax_b.set_xlabel("seconds (sum of cilium-agent bootstrap sub-phases, p50)")
        ax_b.set_title("Cilium agent bootstrap drill-down (T2 -> T3)",
                       fontsize=10, fontweight="bold")
        ax_b.grid(True, axis="x", alpha=0.3)
        ax_b.legend(loc="center left", bbox_to_anchor=(1.02, 0.5),
                    fontsize=7, framealpha=0.95, title="bootstrap phase")
    else:
        ax_b.text(0.5, 0.5, "no cilium bootstrap data in selected runs",
                  ha="center", va="center", transform=ax_b.transAxes,
                  fontsize=10, color="#888")
        ax_b.set_axis_off()

    # --- Panel C: known suspects grouped bars ---
    yc = np.arange(len(rows_c))
    n_sus = len(SUSPECTS)
    width = 0.78 / n_sus
    for i, (sname, scolor) in enumerate(SUSPECTS):
        vals = np.array([r[1][i] for r in rows_c])
        offset = (i - (n_sus - 1) / 2.0) * width
        ax_c.barh(yc + offset, vals, height=width, color=scolor,
                  edgecolor="black", linewidth=0.4, label=sname)
        for j, v in enumerate(vals):
            if v > 0.05:
                ax_c.text(v + 0.2, yc[j] + offset, f"{v:.1f}",
                          va="center", fontsize=7, color="#333")
    ax_c.set_yticks(yc); ax_c.set_yticklabels([r[0] for r in rows_c], fontsize=9)
    ax_c.invert_yaxis()
    ax_c.set_xlabel("seconds (p50)")
    ax_c.set_title("Known suspects — image pull, CNI conflist install, agent-main startup, sched-block taint",
                   fontsize=10, fontweight="bold")
    ax_c.grid(True, axis="x", alpha=0.3)
    ax_c.legend(loc="center left", bbox_to_anchor=(1.02, 0.5),
                fontsize=8, framealpha=0.95, title="suspect")

    fig.suptitle(
        "Networking-only comparison (T1-anchored, IaaS variance excluded)",
        fontsize=12, fontweight="bold", y=0.995,
    )
    fig.tight_layout(rect=(0, 0, 0.83, 0.985))
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "compare_phase_decomposition.png"
    fig.savefig(p, dpi=140); plt.close(fig)
    return p


def plot_compare(csvs: list[Path], out_dir: Path) -> list[Path]:
    """Overlay headline-latency CDF across multiple runs and emit a
    cross-provider phase decomposition figure (cross-provider compare)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 5))
    cdf_metric = "time_to_runnable_s"
    # Fall back to legacy metric if no run carries the new T1-anchored column.
    any_runnable = False
    for csv in csvs:
        try:
            cols = pd.read_csv(csv, nrows=1).columns
        except Exception:
            continue
        if "time_to_runnable_s" in cols:
            any_runnable = True; break
    if not any_runnable:
        cdf_metric = "node_startup_latency_s"
    for csv in csvs:
        df = pd.read_csv(csv)
        ok = _ok(df)
        s = pd.to_numeric(ok.get(cdf_metric), errors="coerce").dropna().sort_values()
        if s.empty:
            continue
        cdf = np.arange(1, len(s) + 1) / len(s)
        label = _run_label(csv)
        ax.plot(s.values, cdf, marker=".", label=label)
    ax.set_xlabel(f"{cdf_metric} (s)"); ax.set_ylabel("CDF")
    ax.set_title(f"{cdf_metric} CDF — provider comparison")
    ax.grid(True, alpha=0.3); ax.legend(fontsize=8)
    p = out_dir / "compare_cdf.png"; fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig)
    out: list[Path] = [p]
    decomp = _plot_compare_phase_decomposition(csvs, out_dir)
    if decomp is not None:
        out.append(decomp)
    return out
