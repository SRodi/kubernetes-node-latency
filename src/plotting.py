"""Matplotlib plots for a single run, and --compare overlays across runs."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .analysis import METRICS


def _pod_basename(pod: str) -> str:
    """Strip ReplicaSet/DaemonSet hash suffix(es) from a pod name so the
    same workload renders as a single group across iterations.

    Iteratively peels trailing ``-<token>`` segments (with optional
    single-letter discriminator like GKE's ``anetd-m-qhlth``). A token is
    treated as a hash only if it has 5–10 ``[a-z0-9]`` chars AND looks
    random — i.e. it contains a digit OR has no vowels. English-like
    suffixes such as ``-agent`` or ``-autoscaler`` are preserved.

    Examples:
      ``anetd-m-qhlth``                                  -> ``anetd``
      ``ip-masq-agent-4ct8j``                            -> ``ip-masq-agent``
      ``event-exporter-gke-7bf86dd5ff-6sfqm``            -> ``event-exporter-gke``
      ``konnectivity-agent-autoscaler-679b575cc9-zndq4`` -> ``konnectivity-agent-autoscaler``
      ``token-broker-adc-init``                          -> ``token-broker-adc-init``
    """
    if not pod:
        return ""
    import re as _re
    _RE = _re.compile(r"(?:-[a-z])?-([a-z0-9]{5,10})$")
    while True:
        m = _RE.search(pod)
        if not m:
            return pod
        tok = m.group(1)
        looks_like_hash = any(c.isdigit() for c in tok) or not any(
            c in "aeiou" for c in tok)
        if not looks_like_hash:
            return pod
        pod = pod[: m.start()]
        if not pod:
            return ""

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
    "init_run":        "#f97316",  # dark orange (init container execution window)
    "trigger_pod":     "#a855f7",  # purple (trigger pod CNI ADD / sandbox setup)
    "kubelet_wait":    "#cbd5e1",  # neutral slate-grey (fallback gap-fill lane)
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


def plot_all(iterations_csv: Path, out_dir: Path, *, title: str = "",
             iteration: int | None = None,
             containers_filter: set[str] | None = None,
             pods_filter: set[str] | None = None,
             filter_out_suffix: str | None = None) -> list[Path]:
    """Generate plots for a run.

    When `iteration` is given (1-indexed), only the phase-profile chart is
    emitted, filtered to that single iteration's row, and saved as
    `phase_profile_iter-{N:03d}.png`. The aggregate plots (box/CDF/etc.)
    require multiple rows and are skipped in single-iteration mode.
    """
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

    if iteration is not None:
        if "iteration" not in ok.columns:
            raise SystemExit("iterations.csv has no 'iteration' column")
        ok_iter = ok[pd.to_numeric(ok["iteration"], errors="coerce") == iteration]
        if ok_iter.empty:
            available = sorted(pd.to_numeric(ok["iteration"], errors="coerce")
                               .dropna().astype(int).unique().tolist())
            raise SystemExit(
                f"iteration {iteration} not found in {iterations_csv.name}; "
                f"available: {available}"
            )
        # Derive title with iteration suffix (re-using the (prov @ region) format)
        prov = ok_iter["provider"].dropna().iloc[0] if "provider" in ok_iter.columns and not ok_iter["provider"].dropna().empty else None
        reg = ok_iter["region"].dropna().iloc[0] if "region" in ok_iter.columns and not ok_iter["region"].dropna().empty else None
        if not title:
            if prov and reg:
                title = f"({prov} @ {reg}) iter {iteration}"
            elif prov:
                title = f"({prov}) iter {iteration}"
            else:
                title = f"iter {iteration}"
        # When a filter is active and no explicit suffix was given,
        # write to `phase_profile_iter-NNN_filtered.png` to avoid
        # overwriting the canonical full chart.
        if containers_filter or pods_filter:
            sfx = filter_out_suffix or "filtered"
            fn = f"phase_profile_iter-{iteration:03d}_{sfx}.png"
        else:
            fn = f"phase_profile_iter-{iteration:03d}.png"
        p = _plot_phase_profile(ok_iter, out_dir, title=title,
                                filename=fn,
                                containers_filter=containers_filter,
                                pods_filter=pods_filter)
        if p is not None:
            paths.append(p)
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
    if containers_filter or pods_filter:
        sfx = filter_out_suffix or "filtered"
        fn = f"phase_profile_{sfx}.png"
    else:
        fn = "phase_profile.png"
    p = _plot_phase_profile(ok, out_dir, title=title, filename=fn,
                            containers_filter=containers_filter,
                            pods_filter=pods_filter)
    if p is not None:
        paths.append(p)

    return paths


def _plot_phase_profile(ok: pd.DataFrame, out_dir: Path, *, title: str,
                        filename: str = "phase_profile.png",
                        containers_filter: set[str] | None = None,
                        pods_filter: set[str] | None = None) -> Path | None:
    """Per-run mean-aligned Gantt of every captured phase.

    Optional filters narrow the chart to specific containers / pods:

    * ``containers_filter`` — set of container names. Lanes survive iff
      their associated container is in this set (looked up via the
      pull/create/run side-maps). Gap-fill ``runtime wait`` lanes
      survive iff their pod has at least one other surviving lane.
    * ``pods_filter`` — set of pod basenames (e.g. ``"azure-cns"``,
      ``"cilium"``). Lanes survive iff their pod basename is in this
      set.

    Both filters compose (intersection). When neither is set, all
    lanes are kept (default behavior).
    """
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
    # Suppressed PROFILE_LANES: replaced by fine-grained per-container
    # phases (sandbox setup is already covered by "Kubelet sandbox +
    # image-pull init"; per-image pulls are rendered as their own rows;
    # per-init runs are appended below as run:<container> lanes).
    _SUPPRESS_MAIN_LANES = {
        "Cilium agent image pull",
        "Cilium init-container chain",
        "Cilium agent bootstrap",
    }
    for label, start_col, end_col, actor in PROFILE_LANES:
        if actor == "cloud":
            continue  # rendered as a numeric annotation, not a bar
        if label in _SUPPRESS_MAIN_LANES:
            continue
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
        # Emit each regeneration sub-phase as its own lane in the main
        # Gantt so the reader sees the breakdown directly (mirrors the
        # cilium bootstrap inlining). `lanes` has already been rebased to
        # T1, so we use T1-relative offsets here too. All lanes share the
        # ``cilium_regen`` actor so they group under the cilium pod box.
        for label, s_rel, e_rel in regen_breakdown:
            if e_rel - s_rel < 5e-3:
                continue
            lanes.append((
                f"regen: {label}", s_rel, e_rel, "cilium_regen",
            ))

    # ---- Image pulls on this node (per-image rows, T1-relative) ----
    # Aggregate across iterations: one row per distinct image, with one bar
    # per pull instance (so the same image pulled across N iterations
    # stacks vertically with N bars on the same y row).
    pulls_rows = _node_image_pulls_aggregated(ok, ok.get("T1_node_registered", pd.Series(dtype=object)))
    image_pulls_by_image: dict[str, list[dict]] = {}
    for p in pulls_rows:
        image_pulls_by_image.setdefault(p["image"], []).append(p)
    # Ordering: sort images by median start_off so the chronologically
    # earliest pulls appear at the top of the sub-panel.
    pull_image_order: list[str] = []
    if image_pulls_by_image:
        pull_image_order = sorted(
            image_pulls_by_image.keys(),
            key=lambda im: float(np.median([r["start_off"] for r in image_pulls_by_image[im]])),
        )
    has_pulls_bd = bool(pull_image_order)
    has_pulls_tl = False  # merged into the main chart as additional lanes

    all_lanes = lanes

    # Side-channel maps populated as lanes are emitted below. Defined
    # early so the cilium init-run lanes (added next) and the non-cilium
    # `run:<container>` lanes (added later) can both write into them.
    pull_lane_keys: dict[str, str] = {}
    pull_label_to_pod: dict[str, str | None] = {}
    pull_label_to_container: dict[str, str | None] = {}
    run_label_to_pod: dict[str, str | None] = {}
    run_label_to_container: dict[str, str | None] = {}
    create_label_to_pod: dict[str, str | None] = {}
    create_label_to_container: dict[str, str | None] = {}
    # `(ns, pod_basename, container) -> median t_created offset (s, T1-relative)`.
    # Populated from node_container_creates_json when present. Looked up
    # when emitting each run lane so we can insert a `create:<container>`
    # sliver between Pulled and Started, closing the CRI gap.
    creates_lookup: dict[tuple[str, str, str], float] = {}
    if "node_container_creates_json" in ok.columns:
        creates_lookup = _aggregate_node_container_creates(ok, ok["T1_node_registered"])

    def _pod_basename_str(pod: str) -> str:
        return _pod_basename(pod or "")

    def _emit_create_lane_for_run(
        ns: str | None, pod_base: str | None, container: str, run_start_off: float,
        run_label: str,
    ) -> None:
        """Insert a ``create:<container>`` lane spanning
        [t_created_off, run_start_off] when we have a matching Created
        event. The lane label is unique per (pod, container) so it groups
        into the right pod box and inherits the container's color.

        When ``ns``/``pod_base`` are None (e.g. the cilium init-chain
        emission site, which only knows container names), we resolve via
        container-name lookup: if exactly one (ns, pod_base, container)
        key in ``creates_lookup`` matches, we use it; ambiguous matches
        are skipped to avoid mis-attribution.
        """
        c_off: float | None = None
        if ns is not None and pod_base is not None:
            c_off = creates_lookup.get((ns or "", pod_base or "", container or ""))
        if c_off is None:
            matches = [(k, v) for k, v in creates_lookup.items() if k[2] == container]
            if len(matches) == 1:
                (mns, mbase, _), c_off = matches[0]
                ns, pod_base = mns, mbase
            else:
                return
        s = c_off
        e = run_start_off
        if e - s < 5e-3:
            return
        pod_key = f"{ns}/{pod_base}" if ns else (pod_base or "")
        base_label = f"create: {container}"
        label = base_label
        dedup = 2
        while label in create_label_to_pod and create_label_to_pod[label] != pod_key:
            label = f"{base_label} ({pod_base})"
            dedup += 1
            if dedup > 10:
                break
        create_label_to_pod[label] = pod_key
        create_label_to_container[label] = container
        all_lanes.append((label, s, e, "init_run"))

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
    # Bridges the (typically small) gap between the end of the cilium-agent
    # image pull (cilium-distroless) and the agent main container reporting
    # T2. We anchor the LEFT edge to the pull's end — not to
    # `last_init_end_t1` — because the agent image pull occupies the
    # interval [last_init_end, t_pulled], so spanning the lane from
    # last_init_end would visually overlap the cilium-distroless pull
    # lane and double-count that time. The lane then represents only the
    # kubelet's create+start residual (sandbox handoff, cgroup setup,
    # process exec). When the agent image is a cache hit or its pull window
    # is missing, fall back to last_init_end so the gap to T2 is still
    # visible. Lanes shorter than 250ms are dropped — anything that short
    # is below the chart's labeling resolution and would just be noise.
    agent_pull_end: float | None = None
    for img, insts in image_pulls_by_image.items():
        if any(r.get("container") == "cilium-agent" for r in insts):
            ends = [r["end_off"] for r in insts if isinstance(r.get("end_off"), (int, float))]
            if ends:
                agent_pull_end = float(np.median(ends))
            break
    if t2_off is not None:
        main_end = t2_off - t1_off
        if agent_pull_end is not None:
            # Agent pull window is the authoritative anchor for the start
            # of the kubelet handover. Use it even if it lands at/after T2
            # (in which case the lane will be dropped by the min-width
            # guard below) — falling back to last_init_end here would
            # re-introduce the visual overlap we just fixed.
            main_start = agent_pull_end
        elif last_init_end_t1 is not None:
            main_start = last_init_end_t1
        else:
            main_start = None
        if main_start is not None and main_end - main_start > 0.25:
            insert_at = len(all_lanes)
            for i, (lbl, _s, _e, _a) in enumerate(all_lanes):
                if lbl == "Cilium agent bootstrap":
                    insert_at = i
                    break
            all_lanes.insert(insert_at, (
                "Agent main container startup",
                main_start, main_end, "kubelet_main",
            ))

    # ---- Per-init-container "run:<name>" lanes ----
    # Replace the coarse "Cilium init-container chain" lane (suppressed
    # above) with one lane per init container so the reader can see the
    # exact execution window of each. Kubelet reports init-container
    # `terminated.started_at` / `finished_at` at 1s resolution, so the
    # naive `finished_at - started_at` rounds to 0-1s for nearly every
    # init and hides the real chain cost. We instead use the time until
    # the NEXT init container starts as the effective duration (the
    # kubelet only transitions to the next init after the current one
    # exits, so successive starts bracket each init's true wall-clock
    # cost). The last init's end is snapped to `last_init_end_t1`
    # (max of last finished_at and T1c) to close the visual gap to the
    # conflist appearing on disk.
    if ic_offsets:
        chain_end = last_init_end_t1 if last_init_end_t1 is not None \
            else max((e for _n, _s, e in ic_offsets), default=None)
        if chain_end is not None:
            ordered = sorted(ic_offsets, key=lambda r: r[1])
            for i, (name, s_rel, e_rel) in enumerate(ordered):
                if i + 1 < len(ordered):
                    next_name = ordered[i + 1][0]
                    next_started = ordered[i + 1][1]
                    # Prefer next container's Created event as the end
                    # of this run lane (kubelet emits Created(N+1) only
                    # after N has exited), so the `create:<next>` lane
                    # — which spans [Created, Started] — doesn't visually
                    # overlap with this run bar.
                    nc_matches = [v for k, v in creates_lookup.items()
                                  if k[2] == next_name]
                    next_created = nc_matches[0] if len(nc_matches) == 1 else None
                    end_rel = next_created if (next_created is not None
                                                and next_created <= next_started
                                                and next_created > s_rel) \
                              else next_started
                else:
                    end_rel = max(e_rel, chain_end)
                end_rel = min(end_rel, chain_end)
                if end_rel <= s_rel:
                    continue
                lbl = f"run: {name}"
                run_label_to_container[lbl] = name
                all_lanes.append((lbl, s_rel, end_rel, "init_run"))
                _emit_create_lane_for_run(None, None, name, s_rel, lbl)

    # ---- Per-container `run:<container>` lanes for non-cilium pods ----
    # `node_container_starts_json` lists every `reason=Started` kubelet
    # event on the new node within the iteration window, tagged with pod
    # + container + init/main. We group by pod basename and emit one
    # `run:<container>` lane per container, using the next container's
    # start (in the same pod) as this lane's end so each step's wall-clock
    # cost is visible (kubelet only transitions to the next container
    # after the current one exits, so successive starts bracket each
    # step at second resolution). The last container in each pod (which
    # is typically the long-running main container) is rendered with a
    # short fixed window so it stays visible without dominating the chart.
    # The cilium pod is skipped — it already has dedicated per-init lanes
    # derived from `init_containers_json` (1s-resolution, but anchored to
    # the full chain end via T1c) plus bootstrap phases.
    if "node_container_starts_json" in ok.columns:
        starts_by_pod = _aggregate_node_container_starts(ok, ok["T1_node_registered"])
        # Identify the cilium DS pod's (ns, base) so the generic loop
        # below skips it — on GKE the DS is named ``anetd``, so the
        # legacy name-only check missed it and emitted duplicate
        # ``run: cilium-agent`` lanes under a second pod group.
        _cilium_ns_base: tuple[str, str] | None = None
        for (_ns, _base), _cs in starts_by_pod.items():
            if any(_c[0] == "cilium-agent" for _c in _cs):
                _cilium_ns_base = (_ns, _base)
                break
        # Container names already rendered as dedicated per-init lanes
        # from `init_containers_json`. We avoid emitting a duplicate
        # `run:` lane for these, but we DO want to recover any cilium-pod
        # container that's missing from `ic_offsets` — i.e. native sidecar
        # init containers (`restartPolicy: Always`, in `state.running`,
        # so kubelet reports no `terminated.started_at` — e.g. GKE
        # anetd's `cni-writer`) and main / sidecar regular containers
        # (`cilium-agent-metrics-collector`, GKE netd-style aux containers
        # if any) that live in `containerStatuses`, not
        # `initContainerStatuses`. Without this, the cilium pod group
        # renders sparse on GKE compared to AKS/EKS.
        _ic_names = {n for n, _s, _e in ic_offsets}
        for (ns, base), container_starts in starts_by_pod.items():
            is_cilium_pod = ((ns, base) == _cilium_ns_base
                             or base == "cilium" or base.startswith("cilium-"))
            if is_cilium_pod:
                # Only emit lanes for containers NOT already covered by
                # the dedicated init-chain rendering. Leave `run_label_to_pod`
                # unset so `_lane_pod_key` falls through to `cilium_pod_key`
                # and groups them under the cilium dashed box.
                filtered = [(c, ii, so) for c, ii, so in container_starts
                            if c not in _ic_names]
                if not filtered:
                    continue
                n = len(filtered)
                for i, (cname, is_init, s_off) in enumerate(filtered):
                    if i + 1 < n:
                        next_started = filtered[i + 1][2]
                        next_cname = filtered[i + 1][0]
                        next_created = creates_lookup.get(
                            (ns or "", base or "", next_cname))
                        e_off = next_created if (next_created is not None
                                                  and next_created <= next_started
                                                  and next_created > s_off) \
                                else next_started
                    else:
                        e_off = s_off + (1.0 if is_init else 2.0)
                    if e_off <= s_off + 5e-3:
                        continue
                    label = f"run: {cname}"
                    run_label_to_container[label] = cname
                    all_lanes.append((label, s_off, e_off, "init_run"))
                    _emit_create_lane_for_run(ns, base, cname, s_off, label)
                continue
            pod_key = f"{ns}/{base}" if ns else base
            n = len(container_starts)
            for i, (cname, is_init, s_off) in enumerate(container_starts):
                if i + 1 < n:
                    next_cname = container_starts[i + 1][0]
                    next_started = container_starts[i + 1][2]
                    # Prefer the next container's Created event as our
                    # end: kubelet only emits Created(N+1) after N has
                    # exited, so using next_started here would visually
                    # overlap this run lane with the `create:<next>`
                    # lane (which spans [Created, Started] of N+1).
                    next_created = creates_lookup.get(
                        (ns or "", base or "", next_cname))
                    e_off = next_created if (next_created is not None
                                              and next_created <= next_started
                                              and next_created > s_off) \
                            else next_started
                else:
                    # Last container in the pod: give it a short visible
                    # window. Init containers without a successor usually
                    # mean the main container's Started event wasn't
                    # captured — use 1s as a conservative end. Main
                    # containers stay running for the rest of the iteration
                    # so 2s is enough to draw a recognizable bar without
                    # implying a specific exit time.
                    e_off = s_off + (1.0 if is_init else 2.0)
                if e_off <= s_off + 5e-3:
                    continue
                base_label = f"run: {cname}"
                label = base_label
                # Disambiguate against any cilium init container with the
                # same name (rare but possible across DSes).
                dedup_n = 2
                while label in run_label_to_pod and run_label_to_pod[label] != pod_key:
                    label = f"{base_label} ({base})"
                    dedup_n += 1
                    if dedup_n > 10:
                        break
                run_label_to_pod[label] = pod_key
                run_label_to_container[label] = cname
                all_lanes.append((label, s_off, e_off, "init_run"))
                _emit_create_lane_for_run(ns, base, cname, s_off, label)

    # ---- Inline Cilium agent bootstrap phases as main-chart lanes ----
    # `cilium_breakdown` already chains the bootstrap phases at their
    # T1-relative positions (anchored so they end exactly at T3). Surface
    # each phase as its own lane so the reader sees every sub-phase on
    # the same time axis as everything else.
    if cilium_breakdown:
        # Insert an "agent: process startup" lane covering the in-process
        # gap between the cilium-agent main container's Started event (T2)
        # and the first instrumented bootstrap phase. The agent binary
        # does Go runtime init, flag parsing, k8s client setup, etc. before
        # logging cilium_bootstrap_seconds["earlyInit"]; without this lane
        # that interval shows up as unexplained whitespace.
        bs_first_start = cilium_breakdown[0][1]
        if t2_off is not None:
            startup_s = t2_off - t1_off
            startup_e = bs_first_start
            if startup_e - startup_s >= 5e-3:
                all_lanes.append((
                    "agent: process startup", startup_s, startup_e, "cilium",
                ))
        for label, s_rel, e_rel in cilium_breakdown:
            if e_rel - s_rel < 5e-3:
                continue
            all_lanes.append((
                f"bootstrap: {label}", s_rel, e_rel, "cilium",
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
    # Note: per-image pull events are rendered as dedicated lanes (one
    # per image, family-coloured) merged into the main chart below, so
    # the pod-scoped Tips/Tip envelope markers are intentionally omitted
    # here to avoid duplication and confusion.
    MAIN_MARKERS = [
        ("T1_node_registered", "T1"),
        ("T_pod_scheduled", "Ts"),
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
    for col, glyph in MAIN_MARKERS:
        off = _mean_offset(col)
        if off is None:
            continue
        markers.append((glyph, off - t1_off))

    # ---- Collapse coincident T-markers into combined labels ----
    # When two or more glyphs land at the same offset (within 1ms), drawing
    # both vertical lines and labels just overlaps illegibly. Instead, keep
    # one "primary" marker (selected by visual prominence) and merge the
    # other glyph names into its label, e.g. ``T1=Ts`` or ``T4=T4b``. The
    # primary is chosen so the more prominent rendering style wins (T4b's
    # bold scheduler line beats T4's dotted line, etc.).
    _GLYPH_PRIORITY = ["T4b", "T5", "T1c", "Tts",
                       "T0", "T1", "T2", "T3", "T4", "Ts", "Tt", "Tcsi",
                       "Tip", "Tips"]

    def _prio(g: str) -> int:
        return _GLYPH_PRIORITY.index(g) if g in _GLYPH_PRIORITY else 99

    _by_off: dict[float, list[str]] = {}
    for _g, _o in markers:
        _by_off.setdefault(round(_o, 3), []).append(_g)
    glyphs_to_skip: set[str] = set()
    combined_label: dict[str, str] = {}
    for _gs in _by_off.values():
        if len(_gs) < 2:
            continue
        _gs_sorted = sorted(_gs, key=_prio)
        _primary = _gs_sorted[0]
        combined_label[_primary] = "=".join(_gs_sorted)
        for _g in _gs_sorted[1:]:
            glyphs_to_skip.add(_g)

    # ---- Merge image-pull rows into the main chart ----
    # Each distinct image pulled on the node becomes its own lane so the
    # reader can see exactly when each pull happens on the same time axis
    # as the lifecycle phases. To keep the chart legible on providers
    # that pull many ancillary images (notably GKE), we:
    #   * drop pulls shorter than MAIN_CHART_PULL_MIN_S (sub-panel still
    #     shows them);
    #   * keep critical-path families per-image (cilium, cns, azure-cni,
    #     trigger) so the reader can see which specific image dominates;
    #   * collapse every other family into ONE aggregated lane covering
    #     `min(start)..max(end)` with a `(xN)` count badge.
    # Labels use just the image basename (last path segment + tag) — the
    # registry/repo prefix is not load-bearing once family colour is set.
    # `pull_lane_keys`, `pull_label_to_pod`, `pull_label_to_container`,
    # `run_label_to_pod`, `run_label_to_container` are defined earlier
    # so cilium init-run lanes can populate them before this block runs.

    if pull_image_order:
        from .image_family import (
            DEFAULT_FAMILY as _DF,
            MAIN_CHART_PER_IMAGE_FAMILIES as _PER_IMG,
            MAIN_CHART_PULL_MIN_S as _MIN_S,
            image_basename as _basename,
        )

        def _pod_key(ns: str, pod: str) -> str | None:
            if not pod:
                return None
            base = _pod_basename(pod)
            return f"{ns}/{base}" if ns else base

        # Detect the cilium-agent pod identifier. Primary path: scan
        # image-pull entries for container=cilium-agent. Fallback: scan
        # container-start entries — on GKE the cilium-agent image is
        # pre-baked into the COS node VHD so no Pulling/Pulled event
        # ever fires; without the fallback ``cilium_pod_key`` would
        # stay None and lifecycle lanes would group under the literal
        # string ``"cilium-agent"`` instead of the real pod (``anetd``).
        cilium_pod_key: str | None = None
        for image, insts in image_pulls_by_image.items():
            for r in insts:
                if r.get("container") == "cilium-agent":
                    cilium_pod_key = _pod_key(r.get("namespace") or "", r.get("pod") or "")
                    break
            if cilium_pod_key:
                break
        if cilium_pod_key is None and "node_container_starts_json" in ok.columns:
            import json as _json
            for v in ok["node_container_starts_json"].dropna():
                try:
                    rows = _json.loads(v) if isinstance(v, str) else []
                except Exception:
                    rows = []
                for r in rows:
                    if r.get("container") == "cilium-agent":
                        cilium_pod_key = _pod_key(r.get("namespace") or "",
                                                  r.get("pod") or "")
                        if cilium_pod_key:
                            break
                if cilium_pod_key:
                    break

        per_image_rows: list[tuple[str, float, float, str, str, str | None, str | None]] = []  # (label, s, e, family, full_ref, pod_key, container)
        agg_by_family_pod: dict[tuple[str, str | None], list[dict]] = {}
        for image in pull_image_order:
            insts = image_pulls_by_image[image]
            fam = insts[0].get("family") or _DF
            s = float(np.median([r["start_off"] for r in insts]))
            e = float(np.median([r["end_off"] for r in insts]))
            dur = e - s
            pod_keys = {
                _pod_key(r.get("namespace") or "", r.get("pod") or "")
                for r in insts
                if r.get("pod")
            }
            pod_keys.discard(None)
            row_pod_key = next(iter(pod_keys)) if len(pod_keys) == 1 else None
            containers = {r.get("container") for r in insts if r.get("container")}
            row_container = next(iter(containers)) if len(containers) == 1 else None
            if fam in _PER_IMG:
                label = _basename(image) or image
                per_image_rows.append((label, s, e, fam, image, row_pod_key, row_container))
            else:
                if dur < _MIN_S:
                    continue
                agg_by_family_pod.setdefault((fam, row_pod_key), []).append(
                    {"image": image, "s": s, "e": e, "pod_key": row_pod_key,
                     "container": row_container})

        agg_rows: list[tuple[str, float, float, str, str, str | None, str | None]] = []
        for (fam, row_pod_key), items in agg_by_family_pod.items():
            s = min(it["s"] for it in items)
            e = max(it["e"] for it in items)
            n = len(items)
            if n == 1:
                label = _basename(items[0]["image"]) or items[0]["image"]
                row_container = items[0].get("container")
            else:
                label = f"{fam} (\u00d7{n})"
                cs = {it.get("container") for it in items if it.get("container")}
                row_container = next(iter(cs)) if len(cs) == 1 else None
            full = ", ".join(_basename(it["image"]) or it["image"] for it in items)
            agg_rows.append((label, s, e, fam, full, row_pod_key, row_container))

        # Emit lanes in chronological order by start.
        merged_rows = sorted(per_image_rows + agg_rows, key=lambda r: r[1])
        for label, s, e, fam, full, pk, container in merged_rows:
            label = f"pull: {label}"
            base = label
            n = 2
            while label in pull_lane_keys and pull_lane_keys[label] != full:
                label = f"{base} ({n})"
                n += 1
            pull_lane_keys[label] = full
            pull_label_to_pod[label] = pk
            pull_label_to_container[label] = container
            all_lanes.append((label, s, e, f"pull:{fam}"))
    else:
        cilium_pod_key = None
        from .image_family import (
            DEFAULT_FAMILY as _DF,
            image_basename as _basename,
            classify as _classify,
        )
        def _pod_key(ns: str, pod: str) -> str | None:
            if not pod:
                return None
            base = _pod_basename(pod)
            return f"{ns}/{base}" if ns else base
        # When no real pull events were captured, recover cilium_pod_key
        # from node_container_starts_json so synthetic pull lanes (below)
        # still group under the cilium box.
        if "node_container_starts_json" in ok.columns:
            import json as _json
            for v in ok["node_container_starts_json"].dropna():
                try:
                    rows = _json.loads(v) if isinstance(v, str) else []
                except Exception:
                    rows = []
                for r in rows:
                    if r.get("container") == "cilium-agent":
                        cilium_pod_key = _pod_key(r.get("namespace") or "",
                                                  r.get("pod") or "")
                        if cilium_pod_key:
                            break
                if cilium_pod_key:
                    break

    # ---- Synthetic `pull:` lanes for cached images (no Pulling event) ----
    # Every run lane should be paired with a `pull:` lane so the reader can
    # see which image each container uses, even when kubelet skipped the
    # Pulling event because the image was already cached on the node
    # (common on GKE COS-baked images and Cilium-distroless sidecars across
    # iterations 2+). We pull (ns, pod, container -> image) from
    # node_container_starts_json (populated by Collector since this commit),
    # find every (pod_key, container) referenced by a run lane that lacks a
    # real pull lane, and insert a zero-width marker right before the run
    # lane's start so it appears as a labeled tick under the right pod box.
    if "node_container_starts_json" in ok.columns:
        from .image_family import (
            DEFAULT_FAMILY as _DF2,
            image_basename as _basename2,
            classify as _classify2,
        )
        import json as _json2
        container_image: dict[tuple[str, str], str] = {}
        for v in ok["node_container_starts_json"].dropna():
            try:
                rows = _json2.loads(v) if isinstance(v, str) else []
            except Exception:
                rows = []
            for r in rows:
                img = r.get("image")
                cn = r.get("container")
                ns = r.get("namespace") or ""
                pod = r.get("pod") or ""
                if not (img and cn and pod):
                    continue
                pk = _pod_key(ns, pod)
                if pk:
                    container_image.setdefault((pk, cn), img)
        # Fallback source: node_image_pulls_json includes (container, image)
        # for every kubelet pull event captured on the new node, even when
        # the pull was dropped from the main chart (e.g. "other" family
        # with sub-second duration, or events where only "Pulled" was
        # observed and `duration_s` is None). This covers legacy runs
        # captured before the `image` field was added to
        # `node_container_starts_json` and any container with a real but
        # filtered-out pull lane (e.g. GKE anetd's `cni-writer`).
        if "node_image_pulls_json" in ok.columns:
            for v in ok["node_image_pulls_json"].dropna():
                try:
                    rows = _json2.loads(v) if isinstance(v, str) else []
                except Exception:
                    rows = []
                for r in rows:
                    img = r.get("image")
                    cn = r.get("container")
                    ns = r.get("namespace") or ""
                    pod = r.get("pod") or ""
                    if not (img and cn and pod):
                        continue
                    pk = _pod_key(ns, pod)
                    if pk:
                        container_image.setdefault((pk, cn), img)

        # (pod_key, container) pairs that already have a pull lane.
        pulled_pairs: set[tuple[str, str]] = set()
        for lbl in pull_label_to_pod:
            pk = pull_label_to_pod.get(lbl)
            cn = pull_label_to_container.get(lbl)
            if pk and cn:
                pulled_pairs.add((pk, cn))

        def _run_pod_key(lbl: str, container: str) -> str | None:
            pk = run_label_to_pod.get(lbl)
            if pk:
                return pk
            # Cilium-pod run lanes don't set run_label_to_pod (they auto-
            # group via _CILIUM_LIFECYCLE_ACTORS -> cilium_pod_key).
            # Resolve by container-name lookup in container_image: when a
            # container name maps to exactly one (pk, cn) entry, use that
            # pk. If ambiguous, fall back to cilium_pod_key.
            matches = {pk for (pk, cn) in container_image if cn == container}
            if len(matches) == 1:
                return next(iter(matches))
            return cilium_pod_key

        synth: list[tuple[str, str, float, str]] = []  # (pk, container, run_start, image)
        seen_synth: set[tuple[str, str]] = set()
        for lbl, s, _e, actor in all_lanes:
            if actor != "init_run" or not lbl.startswith("run: "):
                continue
            cn = run_label_to_container.get(lbl)
            if not cn:
                continue
            pk = _run_pod_key(lbl, cn)
            if not pk:
                continue
            if (pk, cn) in pulled_pairs or (pk, cn) in seen_synth:
                continue
            img = container_image.get((pk, cn))
            if not img:
                continue
            synth.append((pk, cn, s, img))
            seen_synth.add((pk, cn))

        for pk, cn, run_s, img in synth:
            fam = _classify2(img) or _DF2
            base = _basename2(img) or img
            label = f"pull: {base}"
            n = 2
            while label in pull_lane_keys and pull_lane_keys[label] != img:
                label = f"pull: {base} ({n})"
                n += 1
            pull_lane_keys[label] = img
            pull_label_to_pod[label] = pk
            pull_label_to_container[label] = cn
            # Anchor BEFORE the earliest visible event for this container
            # (create lane if present, otherwise the run lane). This
            # preserves the kubelet pipeline ordering Pulled -> Created ->
            # Started even when we only have a zero-width synthetic
            # marker (e.g. cached image, or `t_pulling`/`t_pulled` event
            # that was dropped by per-image filters). Without this the
            # marker visually appears AFTER `create:` because both are
            # near `run_start` and the create lane spans
            # [t_created, t_started].
            anchor = run_s
            if "/" in pk:
                _ns_pk, _base_pk = pk.split("/", 1)
            else:
                _ns_pk, _base_pk = "", pk
            c_off = creates_lookup.get((_ns_pk, _base_pk, cn))
            if c_off is None:
                _m = [v for k, v in creates_lookup.items()
                      if k[2] == cn and (k[0], k[1]) == (_ns_pk, _base_pk)]
                if _m:
                    c_off = _m[0]
            if c_off is not None and c_off < anchor:
                anchor = c_off
            s = max(0.0, anchor - 5e-3)
            e = max(s + 5e-3, anchor)
            all_lanes.append((label, s, e, f"pull:{fam}"))

    # ---- Fill remaining per-pod gaps with `kubelet: sync wait` lanes ----
    # Now that ALL explainable lanes are emitted (pull/create/run/
    # bootstrap/agent-startup/etc.), walk each pod's lanes in time
    # order. Any contiguous gap >= _GAP_FILL_MIN_S is filled by a
    # synthetic neutral lane attributing the time to kubelet's SyncPod
    # loop / serialized image-pull queue contention. This makes the
    # ~10-20s "unexplained" gaps observed on busy fresh nodes
    # (e.g. azure-ipam Pulled -> cni-installer Created) visible and
    # correctly attributed to kubelet, not to the container itself.
    _GAP_FILL_MIN_S = 0.75

    def _lane_pod_key_for_gapfill(lbl: str, actor: str) -> str | None:
        if isinstance(actor, str) and actor.startswith("pull:"):
            return pull_label_to_pod.get(lbl)
        if actor == "init_run":
            return (create_label_to_pod.get(lbl)
                    or run_label_to_pod.get(lbl)
                    or (cilium_pod_key or "cilium-agent"))
        if actor in {"cilium", "kubelet_main", "image_pull", "cni",
                     "cilium_regen"}:
            return cilium_pod_key or "cilium-agent"
        return None

    _pod_intervals: dict[str, list[tuple[float, float]]] = {}
    for _lbl, _s, _e, _a in all_lanes:
        pk = _lane_pod_key_for_gapfill(_lbl, _a)
        if pk is None:
            continue
        _pod_intervals.setdefault(pk, []).append((_s, _e))
    _wait_label_counter = 0
    for pk, intervals in _pod_intervals.items():
        intervals.sort()
        cursor = intervals[0][0]
        for s, e in intervals:
            gap = s - cursor
            if gap >= _GAP_FILL_MIN_S:
                _wait_label_counter += 1
                pod_short = pk.split("/")[-1]
                # Honest label: we know SOMETHING delayed kubelet from
                # progressing this pod, but we can't always attribute it
                # without event/trace data. "runtime/kubelet wait" leaves
                # the actor ambiguous between CRI (containerd snapshotter
                # / disk I/O) and kubelet (PLEG cadence, sync loop). The
                # node_pod_events capture (when present) reveals the
                # specific reason; absent that, the cause is most often
                # CRI runtime latency on a busy fresh node.
                label = f"runtime wait: {pod_short}"
                if (label in create_label_to_pod
                        or label in run_label_to_pod
                        or label in pull_label_to_pod):
                    label = f"runtime wait: {pod_short} #{_wait_label_counter}"
                # Route through create_label_to_pod so pod-grouping and
                # _lane_pod_key pick up the pod assignment.
                create_label_to_pod[label] = pk
                all_lanes.append((label, cursor, s, "kubelet_wait"))
            cursor = max(cursor, e)

    # ---- Apply --containers / --pods filter ----
    # When the caller passed a container or pod whitelist, prune
    # all_lanes to lanes that match. Gap-fill `runtime wait` lanes
    # survive iff their pod has at least one other surviving lane
    # (otherwise they'd float alone, attributing time to a pod that
    # was entirely removed from the chart). T-marker vertical lines
    # are drawn later from `_mean_offset()` directly and are NOT
    # affected by this filter — they remain global timeline anchors.
    if containers_filter or pods_filter:
        _CILIUM_LIFECYCLE_ACTORS_FILTER = {
            "image_pull", "cni", "cilium", "kubelet_main",
            "cilium_regen", "init_run",
        }

        def _lane_container_f(lbl: str, actor: str) -> str | None:
            if isinstance(actor, str) and actor.startswith("pull:"):
                return pull_label_to_container.get(lbl)
            if actor == "init_run":
                return (create_label_to_container.get(lbl)
                        or run_label_to_container.get(lbl))
            return None

        def _lane_pod_base_f(lbl: str, actor: str) -> str | None:
            pk = None
            if isinstance(actor, str) and actor.startswith("pull:"):
                pk = pull_label_to_pod.get(lbl)
            elif actor in ("init_run", "kubelet_wait"):
                pk = (create_label_to_pod.get(lbl)
                      or run_label_to_pod.get(lbl))
            if pk is None and actor in _CILIUM_LIFECYCLE_ACTORS_FILTER:
                pk = cilium_pod_key
            if not pk:
                return None
            return pk.split("/")[-1]

        _filter_pass1: list[tuple[str, float, float, str]] = []
        for lane in all_lanes:
            lbl, _s, _e, actor = lane
            if actor == "kubelet_wait":
                _filter_pass1.append(lane)
                continue
            c = _lane_container_f(lbl, actor)
            p = _lane_pod_base_f(lbl, actor)
            keep = True
            if containers_filter:
                keep = keep and (c in containers_filter)
            if pods_filter:
                keep = keep and (p in pods_filter)
            if keep:
                _filter_pass1.append(lane)

        _surviving_pods: set[str] = set()
        for lbl, _s, _e, actor in _filter_pass1:
            if actor == "kubelet_wait":
                continue
            p = _lane_pod_base_f(lbl, actor)
            if p:
                _surviving_pods.add(p)

        all_lanes = [
            lane for lane in _filter_pass1
            if lane[3] != "kubelet_wait"
            or _lane_pod_base_f(lane[0], lane[3]) in _surviving_pods
        ]

        if not all_lanes:
            print(
                f"phase_profile filter matched no lanes: "
                f"containers={sorted(containers_filter or [])} "
                f"pods={sorted(pods_filter or [])}"
            )
            return None

        # Narrow the image-pull sub-panel to only the images whose pull
        # lane survived the filter, so the chart below reflects the same
        # pods/containers as the main Gantt. `pull_lane_keys` maps the
        # rendered pull label to the canonical image string used in
        # `pull_image_order` / `image_pulls_by_image`.
        _surviving_images: set[str] = set()
        for lbl, _s, _e, actor in all_lanes:
            if isinstance(actor, str) and actor.startswith("pull:"):
                img = pull_lane_keys.get(lbl)
                if img:
                    # Aggregated rows may map a label to a comma-separated
                    # "img1, img2" string (see agg_rows in pull emission);
                    # split so each constituent image is retained.
                    for part in img.split(","):
                        part = part.strip()
                        if part:
                            _surviving_images.add(part)
        if _surviving_images:
            pull_image_order = [im for im in pull_image_order
                                if im in _surviving_images
                                or (_basename2(im) or im) in _surviving_images]
            image_pulls_by_image = {im: image_pulls_by_image[im]
                                    for im in pull_image_order
                                    if im in image_pulls_by_image}
        else:
            pull_image_order = []
            image_pulls_by_image = {}
        has_pulls_bd = bool(pull_image_order)

    # Layout: main wall-clock Gantt on top, plus optional zoomed subplots
    # (CNI add\u2192Ready decomposition, Cilium-internal bootstrap).
    has_cilium_bd = False  # bootstrap phases inlined into main chart
    has_cni_bd = False     # init-container chain inlined as run:<name> lanes
    has_regen_bd = False   # endpoint regen lane inlined into main chart
    n_main = len(all_lanes)
    n_pull_rows = len(pull_image_order)
    sub_count = (
        (1 if has_cni_bd else 0)
        + (1 if has_cilium_bd else 0)
        + (1 if has_regen_bd else 0)
        + (1 if has_pulls_bd else 0)
        + (1 if has_pulls_tl else 0)
    )
    # Pull sub-panel height scales with number of distinct images.
    pulls_ratio = max(2.0, min(0.6 * n_pull_rows + 1.0, 8.0)) if has_pulls_bd else 0.0
    pulls_tl_ratio = pulls_ratio if has_pulls_tl else 0.0
    fig_height = (
        4 + 0.35 * n_main
        + 2.0 * (sub_count - (1 if has_pulls_bd else 0) - (1 if has_pulls_tl else 0))
        + (pulls_ratio + pulls_tl_ratio) * 0.9
    )
    if sub_count:
        ratios = [max(n_main, 4)]
        if has_cni_bd:
            ratios.append(2.0)
        if has_cilium_bd:
            ratios.append(2.0)
        if has_regen_bd:
            ratios.append(2.0)
        if has_pulls_bd:
            ratios.append(pulls_ratio)
        if has_pulls_tl:
            ratios.append(pulls_tl_ratio)
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
    ax_pulls = sub_axes.pop(0) if has_pulls_bd else None
    ax_pulls_tl = sub_axes.pop(0) if has_pulls_tl else None

    # ---- Enforce pull <= create <= run ordering per (pod, container) ----
    # Kubelet pulled/created/started timestamps usually obey this order,
    # but clock-second rounding, missing `Pulling` events (only `Pulled`
    # captured), and our synthetic zero-width pull markers can produce
    # configurations where a `pull:` lane's start is >= a `create:` or
    # `run:` lane's start for the same container. That visually places
    # pull after create/run in the per-pod chronological sort below.
    # Walk every (pod, container) triple and shift the pull lane's
    # `[s, e]` backwards (preserving width) so its start is strictly
    # earlier than the earliest of its sibling create/run lanes.
    _by_pc: dict[tuple[str, str], dict[str, list[int]]] = {}
    for _i, (_lbl, _s, _e, _a) in enumerate(all_lanes):
        if isinstance(_a, str) and _a.startswith("pull:"):
            _pk = pull_label_to_pod.get(_lbl)
            _cn = pull_label_to_container.get(_lbl)
            _kind = "pull"
        elif _a == "init_run" and _lbl in create_label_to_pod:
            _pk = create_label_to_pod.get(_lbl)
            _cn = create_label_to_container.get(_lbl)
            _kind = "create"
        elif _a == "init_run" and _lbl in run_label_to_pod:
            _pk = run_label_to_pod.get(_lbl)
            _cn = run_label_to_container.get(_lbl)
            _kind = "run"
        elif _a == "init_run" and _lbl.startswith("run: "):
            # Cilium-pod run lane (no run_label_to_pod entry); group by
            # container name resolution to cilium_pod_key.
            _cn = run_label_to_container.get(_lbl)
            _pk = cilium_pod_key if _cn else None
            _kind = "run"
        else:
            continue
        if not (_pk and _cn):
            continue
        _by_pc.setdefault((_pk, _cn), {}).setdefault(_kind, []).append(_i)

    _EPS = 5e-3
    for (_pk, _cn), _kinds in _by_pc.items():
        _pull_is = _kinds.get("pull") or []
        _create_is = _kinds.get("create") or []
        _run_is = _kinds.get("run") or []
        if not _pull_is:
            continue
        _earliest_sibling = None
        for _i in _create_is + _run_is:
            _s = all_lanes[_i][1]
            if _earliest_sibling is None or _s < _earliest_sibling:
                _earliest_sibling = _s
        if _earliest_sibling is None:
            continue
        for _i in _pull_is:
            _lbl, _s, _e, _a = all_lanes[_i]
            if _s + _EPS > _earliest_sibling:
                _w = max(_e - _s, _EPS)
                _new_e = max(0.0, _earliest_sibling - _EPS / 2)
                _new_s = max(0.0, _new_e - _w)
                all_lanes[_i] = (_lbl, _new_s, _new_e, _a)
        # Also ensure create lanes don't start strictly after run.
        if _create_is and _run_is:
            _run_start = min(all_lanes[_i][1] for _i in _run_is)
            for _i in _create_is:
                _lbl, _s, _e, _a = all_lanes[_i]
                if _s > _run_start:
                    _w = max(_e - _s, _EPS)
                    _new_s = max(0.0, _run_start - _w)
                    all_lanes[_i] = (_lbl, _new_s, _run_start, _a)

    # ---- Chronological sort of all lanes ----
    # Before the pod-grouping reorder below, sort by start time so that
    # (a) within each pod's box, lanes appear in the order they actually
    # happened, and (b) across pods, the first lane of each pod tracks
    # the pod's first event. This makes "pull-for-X then run-of-X" pairs
    # appear adjacent and in the right order — e.g. azure-iptables-monitor
    # image pull immediately precedes run:iptables-blocker-init.
    all_lanes.sort(key=lambda r: r[1])

    # ---- Reorder lanes so same-pod lanes are contiguous ----
    # Chronological ordering scatters a pod's lanes (e.g. cilium lifecycle
    # lanes near the top + cilium image-pull rows at the bottom) which
    # forces the container-grouping box to either engulf unrelated lanes
    # or split into multiple boxes. Clumping a pod's lanes together at
    # the position of its FIRST lane lets each pod render as a single
    # tight outer box while keeping the relative chronology within the
    # pod intact and leaving non-pod lifecycle lanes in their original
    # positions.
    _CILIUM_LIFECYCLE_ACTORS = {"image_pull", "cni", "cilium",
                                 "kubelet_main", "cilium_regen",
                                 "init_run"}

    def _lane_pod_key(lbl: str, actor: str) -> str | None:
        if isinstance(actor, str) and actor.startswith("pull:"):
            return pull_label_to_pod.get(lbl)
        if actor == "kubelet_wait":
            # Gap-fill lanes — pod_key stashed in create_label_to_pod
            # at emission time.
            return create_label_to_pod.get(lbl)
        if actor == "init_run" and lbl in create_label_to_pod:
            # create:<container> lane (CRI prep window) — group with
            # its owning pod just like run lanes.
            return create_label_to_pod[lbl]
        if actor == "init_run" and lbl in run_label_to_pod:
            # Non-cilium pod's run:<container> lane — use the explicit
            # pod_key we recorded when the lane was emitted.
            return run_label_to_pod[lbl]
        if actor in _CILIUM_LIFECYCLE_ACTORS:
            return cilium_pod_key or "cilium-agent"
        if actor == "trigger_pod":
            return "trigger-pod"
        return None

    _orig_keys = [_lane_pod_key(lbl, actor) for lbl, _s, _e, actor in all_lanes]
    _emitted = [False] * len(all_lanes)
    _reordered: list[tuple[str, float, float, str]] = []
    _reordered_keys: list[str | None] = []
    for i, lane in enumerate(all_lanes):
        if _emitted[i]:
            continue
        key = _orig_keys[i]
        if key is None:
            _reordered.append(lane)
            _reordered_keys.append(None)
            _emitted[i] = True
        else:
            for j in range(i, len(all_lanes)):
                if not _emitted[j] and _orig_keys[j] == key:
                    _reordered.append(all_lanes[j])
                    _reordered_keys.append(key)
                    _emitted[j] = True
    all_lanes = _reordered
    lane_pod_keys = _reordered_keys

    y_positions = np.arange(n_main, 0, -1)  # top-down listing
    used_actors: list[str] = []
    used_pull_fams: list[str] = []
    from .image_family import FAMILY_COLORS as _FC, DEFAULT_FAMILY as _DF2

    # ---- Per-container color map ----
    # Pair each pull lane (image) with the container that consumes the
    # image, and each `run:<container>` lane with its container, so both
    # bars share a single colour. The colour palette is assigned in the
    # chronological order each container first appears on the chart, so
    # adjacent bars get visually-grouped hues. Aggregated multi-image pull
    # lanes (no single container) keep their family colour.
    _CONTAINER_PALETTE = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#ff9896",
        "#aec7e8", "#c5b0d5", "#98df8a", "#ffbb78", "#9edae5",
    ]
    container_first_off: dict[str, float] = {}
    for lbl, s, _e, actor in all_lanes:
        c: str | None
        if isinstance(actor, str) and actor.startswith("pull:"):
            c = pull_label_to_container.get(lbl)
        elif actor == "init_run":
            c = create_label_to_container.get(lbl) or run_label_to_container.get(lbl)
        else:
            c = None
        if c and c not in container_first_off:
            container_first_off[c] = s
    container_to_color: dict[str, str] = {}
    for i, c in enumerate(sorted(container_first_off, key=container_first_off.get)):
        container_to_color[c] = _CONTAINER_PALETTE[i % len(_CONTAINER_PALETTE)]
    used_containers: list[str] = []

    for (label, s_off, e_off, actor), y in zip(all_lanes, y_positions):
        dur = e_off - s_off
        is_pull = isinstance(actor, str) and actor.startswith("pull:")
        # Look up the container this lane is "for" (1:1 mapping for both
        # the image pull and the subsequent run). Container colour wins
        # over family / actor colour when present.
        if is_pull:
            container = pull_label_to_container.get(label)
        elif actor == "init_run":
            container = create_label_to_container.get(label) or run_label_to_container.get(label)
        else:
            container = None
        if container and container in container_to_color:
            color = container_to_color[container]
            if container not in used_containers:
                used_containers.append(container)
            bar_h = 0.45 if is_pull else 0.55
        elif is_pull:
            fam = actor.split(":", 1)[1] or _DF2
            color = _FC.get(fam, _FC[_DF2])
            if fam not in used_pull_fams:
                used_pull_fams.append(fam)
            bar_h = 0.45
        else:
            color = ACTOR_COLORS.get(actor, "#888888")
            bar_h = 0.55
            if actor not in used_actors:
                used_actors.append(actor)
        ax.barh(y, max(dur, 0.05), left=s_off, height=bar_h, color=color,
                edgecolor="black", linewidth=0.5,
                alpha=0.9 if is_pull else 1.0)
        text = f"{dur:.2f}s"
        ax.text(s_off + dur / 2 if dur > 1.5 else e_off + 0.4, y, text,
                va="center", ha="center" if dur > 1.5 else "left",
                fontsize=7 if is_pull else 8,
                color="white" if (dur > 1.5 and not is_pull) else "black")

    # ---- Container/pod grouping boxes ----
    # Lanes have already been reordered so each pod's lanes are
    # contiguous. Draw a single thin dashed outer border around the
    # block belonging to each pod.
    pod_groups: dict[str, list[int]] = {}
    for i, key in enumerate(lane_pod_keys):
        if key:
            pod_groups.setdefault(key, []).append(i)

    from matplotlib.patches import Rectangle as _Rect
    _box_palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd",
                    "#8c564b", "#e377c2", "#17becf", "#bcbd22"]
    sorted_groups = sorted(
        pod_groups.items(),
        key=lambda kv: -max(y_positions[i] for i in kv[1]),
    )
    for gi, (key, idxs) in enumerate(sorted_groups):
        if len(idxs) < 2:
            continue
        ys = [y_positions[i] for i in idxs]
        bars = [all_lanes[i] for i in idxs]
        y_top = max(ys) + 0.45
        y_bot = min(ys) - 0.45
        x_left = min(b[1] for b in bars)
        x_right = max(b[2] for b in bars)
        span = max(x_right - x_left, 0.5)
        x_pad = span * 0.015 + 0.15
        color = _box_palette[gi % len(_box_palette)]
        short_key = key.split("/", 1)[-1] if "/" in key else key
        rect = _Rect(
            (x_left - x_pad, y_bot),
            (x_right - x_left) + 2 * x_pad,
            y_top - y_bot,
            fill=False, edgecolor=color, linewidth=1.3, linestyle="--",
            zorder=0.5, alpha=0.9,
        )
        ax.add_patch(rect)
        ax.text(x_right + x_pad, y_top, f"\u2192 {short_key}",
                ha="left", va="bottom", fontsize=7,
                color=color, fontweight="bold")

    for glyph, off in markers:
        if glyph in glyphs_to_skip:
            continue
        # Render T4b prominently: it marks when the node becomes schedulable
        # (cilium taint removed) and is the primary user-facing readiness
        # outcome of the run.
        if glyph == "T4b":
            ax.axvline(off, color=ACTOR_COLORS["scheduler"], linestyle="-",
                       alpha=0.85, linewidth=1.6)
            _g_label = combined_label.get("T4b", "T4b")
            label = f"{_g_label}\n(pod schedulable)"
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
            _g_label = combined_label.get("T5", "T5")
            ax.annotate(
                f"{_g_label}\n(pod running)",
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
            if "Tts" in combined_label:
                ax.text(off, n_main + 0.6, combined_label["Tts"],
                        ha="center", va="bottom", fontsize=8,
                        color=ACTOR_COLORS["trigger_pod"], fontweight="bold")
        elif glyph == "T1c":
            # T1c (conflist written / discovered on disk) is a key transition
            # marker: kubelet stops reporting "no CNI" after this point.
            # Render it as a colored dashed line so the reader can see how
            # it relates to the init-container chain on the lane below
            # (especially on managed-Cilium variants where T1c falls
            # mid-chain because the conflist is pre-baked into the image).
            ax.axvline(off, color="#b91c1c", linestyle="--", alpha=0.85,
                       linewidth=1.2)
            ax.text(off, n_main + 0.6, combined_label.get(glyph, glyph),
                    ha="center", va="bottom",
                    fontsize=8, color="#b91c1c", fontweight="bold")
        else:
            ax.axvline(off, color="black", linestyle=":", alpha=0.35, linewidth=0.8)
            ax.text(off, n_main + 0.6, combined_label.get(glyph, glyph),
                    ha="center", va="bottom",
                    fontsize=8, color="black")

    ax.set_yticks(y_positions)
    ax.set_yticklabels([label for label, _, _, _ in all_lanes], fontsize=9)
    ax.set_xlabel("seconds since T1 (node registered)")
    ax.set_ylim(0.2, n_main + 1.2)
    # Lanes are T1-relative; clamp left edge to 0 so synthetic markers
    # that anchor a few ms before run-start don't push the origin
    # negative.
    _xr = ax.get_xlim()[1]
    ax.set_xlim(left=0, right=_xr if _xr > 0 else None)
    ax.margins(x=0)

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
    if used_pull_fams:
        legend_handles += [
            Patch(facecolor=_FC.get(f, _FC[_DF2]), edgecolor="black",
                  label=f"pull:{f}")
            for f in used_pull_fams
        ]
    ax.legend(handles=legend_handles, loc="lower left", fontsize=8, title="actor",
              ncol=2 if (len(used_actors) + len(used_pull_fams)) > 6 else 1)

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

    # ---- Image pulls on this node (per-image median duration) ----
    if has_pulls_bd and ax_pulls is not None:
        from .image_family import FAMILY_COLORS, DEFAULT_FAMILY
        # Sort distinct images by median pull duration (longest first) so
        # the cost ranking is obvious at a glance. Within each row we draw
        # one bold bar (median) plus a thin IQR whisker (p25..p75) when
        # the image was pulled more than once across iterations.
        per_image: list[dict] = []
        for image in pull_image_order:
            insts = image_pulls_by_image[image]
            durs = [float(i["duration_s"]) for i in insts]
            per_image.append({
                "image": image,
                "family": insts[0].get("family") or DEFAULT_FAMILY,
                "med": float(np.median(durs)),
                "p25": float(np.percentile(durs, 25)),
                "p75": float(np.percentile(durs, 75)),
                "count": len(insts),
                "min": min(durs),
                "max": max(durs),
            })
        per_image.sort(key=lambda d: -d["med"])
        n_pull_rows = len(per_image)
        # Top-down listing (longest at top).
        y_pulls = np.arange(n_pull_rows, 0, -1)
        used_fams: list[str] = []

        from .image_family import image_basename as _basename
        max_dur = max((d["max"] for d in per_image), default=1.0)
        for y, d in zip(y_pulls, per_image):
            fam = d["family"]
            color = FAMILY_COLORS.get(fam, FAMILY_COLORS[DEFAULT_FAMILY])
            if fam not in used_fams:
                used_fams.append(fam)
            ax_pulls.barh(y, d["med"], height=0.62, color=color,
                          edgecolor="black", linewidth=0.5)
            # IQR whisker if the pull happened multiple times.
            if d["count"] > 1 and d["p75"] > d["p25"]:
                ax_pulls.errorbar(
                    d["med"], y,
                    xerr=[[max(d["med"] - d["p25"], 0)],
                          [max(d["p75"] - d["med"], 0)]],
                    fmt="none", ecolor="#111", capsize=3, linewidth=1.0,
                )
            # Time label sits just past the bar — the dominant signal.
            cnt = f"  (×{d['count']})" if d["count"] > 1 else ""
            ax_pulls.text(d["med"] + max_dur * 0.012, y,
                          f"{d['med']:.2f}s{cnt}",
                          va="center", ha="left", fontsize=8,
                          color="#111", fontweight="bold")

        ax_pulls.set_yticks(y_pulls)
        ax_pulls.set_yticklabels([_basename(d["image"]) or d["image"] for d in per_image],
                                 fontsize=7)
        # Force the x-axis origin at 0: matplotlib's autoscaler can add a
        # small negative margin when errorbar whiskers touch x=0, which
        # makes the visualization appear to start below 0s.
        ax_pulls.set_xlim(left=0, right=max(max_dur * 1.22, 0.1))
        ax_pulls.margins(x=0)
        ax_pulls.set_ylim(0.3, n_pull_rows + 0.7)
        ax_pulls.set_xlabel("pull duration (seconds, median across iterations; whisker = p25..p75)")
        # Headline stats for the title. Recompute from the dedup
        # aggregator data so old CSVs (whose `image_pulls_total_s` may
        # include cache-hit duplicates) still produce a title number
        # matching what the bars show.
        critical_p50 = _mean_offset_metric(ok, "image_pulls_critical_s")
        # Sum-of-durations per iteration, using dedup'd per-image entries.
        per_iter_total: dict = {}
        for d in pulls_rows:
            per_iter_total.setdefault(d["iteration_idx"], 0.0)
            per_iter_total[d["iteration_idx"]] += float(d["duration_s"])
        total_p50 = float(np.median(list(per_iter_total.values()))) if per_iter_total else None
        crit_str = f"{critical_p50:.2f}s" if critical_p50 is not None else "n/a"
        tot_str = f"{total_p50:.2f}s" if total_p50 is not None else "n/a"
        ax_pulls.set_title(
            f"{title + '  ' if title else ''}"
            f"Image pulls on this node — {n_pull_rows} distinct images, "
            f"critical-path p50 = {crit_str}, sum-of-durations p50 = {tot_str}",
            fontsize=9, loc="left",
        )
        ax_pulls.grid(True, axis="x", alpha=0.3)
        ax_pulls.invert_yaxis() if False else None  # listing is already top-down via y_pulls
        # Per-family legend.
        from matplotlib.patches import Patch
        fam_handles = [
            Patch(facecolor=FAMILY_COLORS.get(f, FAMILY_COLORS[DEFAULT_FAMILY]),
                  edgecolor="black", label=f)
            for f in used_fams
        ]
        ax_pulls.legend(handles=fam_handles, loc="lower right", fontsize=7,
                        title="family", ncol=min(len(fam_handles), 4))

    # ---- Image-pull TIMELINE (when each pull happens, T1-relative) ----
    if has_pulls_tl and ax_pulls_tl is not None:
        from .image_family import FAMILY_COLORS, DEFAULT_FAMILY
        # Sort rows chronologically (by median start offset) so the reader
        # can see the cascade of pulls.
        chrono: list[dict] = []
        for image in pull_image_order:
            insts = image_pulls_by_image[image]
            chrono.append({
                "image": image,
                "family": insts[0].get("family") or DEFAULT_FAMILY,
                "med_start": float(np.median([i["start_off"] for i in insts])),
                "med_end": float(np.median([i["end_off"] for i in insts])),
                "instances": insts,
            })
        chrono.sort(key=lambda d: d["med_start"])
        n_tl = len(chrono)
        y_tl = np.arange(n_tl, 0, -1)
        used_fams_tl: list[str] = []
        all_s: list[float] = []
        all_e: list[float] = []

        def _short_tl(ref: str, maxlen: int = 60) -> str:
            if len(ref) <= maxlen:
                return ref
            if "/" in ref:
                tail = ref.rsplit("/", 1)[-1]
                if len(tail) <= maxlen - 4:
                    return ".../" + tail
            return ref[: maxlen - 1] + "\u2026"

        for y, d in zip(y_tl, chrono):
            fam = d["family"]
            color = FAMILY_COLORS.get(fam, FAMILY_COLORS[DEFAULT_FAMILY])
            if fam not in used_fams_tl:
                used_fams_tl.append(fam)
            n_inst = len(d["instances"])
            jitter_h = 0.6 if n_inst == 1 else max(0.6 / n_inst, 0.10)
            for i, inst in enumerate(d["instances"]):
                s = inst["start_off"]; e = inst["end_off"]
                dur = max(e - s, 0.05)
                y_pos = y if n_inst == 1 else y + (i - (n_inst - 1) / 2) * jitter_h
                ax_pulls_tl.barh(y_pos, dur, left=s, height=jitter_h,
                                 color=color, edgecolor="black", linewidth=0.3,
                                 alpha=0.9)
                all_s.append(s); all_e.append(e)

        ax_pulls_tl.set_yticks(y_tl)
        ax_pulls_tl.set_yticklabels([_short_tl(d["image"]) for d in chrono],
                                    fontsize=7)
        # Overlay T1/T1c/T2/T3/T4 vertical reference lines.
        overlay = [
            (0.0, "T1", "black"),
            (t1c_rel, "T1c", "#b91c1c"),
            ((t2_off - t1_off) if t2_off is not None else None, "T2", "#16a34a"),
            ((t3_off - t1_off) if t3_off is not None else None, "T3", "#15803d"),
            ((t4_off - t1_off) if t4_off is not None else None, "T4", "#475569"),
        ]
        for x, glyph, col in overlay:
            if x is None:
                continue
            ax_pulls_tl.axvline(x, color=col, linestyle="--", alpha=0.6, linewidth=1.0)
            ax_pulls_tl.text(x, n_tl + 0.4, glyph, ha="center", va="bottom",
                             fontsize=8, color=col, fontweight="bold")
        if all_s and all_e:
            # Share x-axis range with the main top chart so each pull aligns
            # vertically with the lifecycle phases happening at the same
            # T1-relative timestamps. Without this the timeline auto-scales
            # to only the pull window and creates a misleading impression
            # that pulls happen earlier than they do.
            main_lo, main_hi = ax.get_xlim()
            span = max(all_e) - min(all_s)
            pad = max(span * 0.04, 0.5)
            lo = min(main_lo, min(all_s) - pad)
            hi = max(main_hi, max(all_e) + pad)
            ax_pulls_tl.set_xlim(lo, hi)
        ax_pulls_tl.set_ylim(0.3, n_tl + 1.0)
        ax_pulls_tl.set_xlabel("seconds since T1 (node registered)")
        ax_pulls_tl.set_title(
            f"{title + '  ' if title else ''}"
            f"Image-pull timeline — when each pull happens (sorted by median start)",
            fontsize=9, loc="left",
        )
        ax_pulls_tl.grid(True, axis="x", alpha=0.3)
        from matplotlib.patches import Patch
        fam_handles_tl = [
            Patch(facecolor=FAMILY_COLORS.get(f, FAMILY_COLORS[DEFAULT_FAMILY]),
                  edgecolor="black", label=f)
            for f in used_fams_tl
        ]
        ax_pulls_tl.legend(handles=fam_handles_tl, loc="lower right", fontsize=7,
                           title="family", ncol=min(len(fam_handles_tl), 4))

    p = out_dir / filename
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


def _aggregate_node_container_starts(
    ok: pd.DataFrame, t1_off_per_row: pd.Series,
) -> dict[tuple[str, str], list[tuple[str, bool, float]]]:
    """Aggregate per-container ``Started`` events across iterations.

    Returns a dict keyed by ``(namespace, pod_basename)`` whose value is a
    list of ``(container_name, init: bool, median_start_offset_s)`` ordered
    by median start offset. The pod basename is the pod name with its
    trailing ``-xxxxx`` ReplicaSet/DaemonSet hash suffix(es) stripped, so
    rows aggregate cleanly across iterations even when each iteration sees
    a freshly-named pod.
    """
    if "node_container_starts_json" not in ok.columns:
        return {}
    import json as _json
    from datetime import datetime as _dt
    t1_series = pd.to_datetime(t1_off_per_row, utc=True, errors="coerce")
    # key = (ns, pod_basename, container, init) -> list[start_offset_s]
    starts: dict[tuple[str, str, str, bool], list[float]] = {}
    _basename = _pod_basename
    for raw, t1 in zip(ok["node_container_starts_json"], t1_series):
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        if pd.isna(t1):
            continue
        try:
            entries = _json.loads(raw)
        except Exception:
            continue
        for e in entries:
            ts = e.get("t_started")
            if not ts:
                continue
            try:
                ts_t = _dt.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            ns = e.get("namespace") or ""
            pod = e.get("pod") or ""
            container = e.get("container") or ""
            if not (ns and pod and container):
                continue
            is_init = bool(e.get("init", False))
            base = _basename(pod)
            s_off = (ts_t - t1.to_pydatetime()).total_seconds()
            starts.setdefault((ns, base, container, is_init), []).append(s_off)
    by_pod: dict[tuple[str, str], list[tuple[str, bool, float]]] = {}
    for (ns, base, container, is_init), offs in starts.items():
        med = float(pd.Series(offs).median())
        by_pod.setdefault((ns, base), []).append((container, is_init, med))
    for k in by_pod:
        by_pod[k].sort(key=lambda r: r[2])
    return by_pod


def _aggregate_node_container_creates(
    ok: pd.DataFrame, t1_off_per_row: pd.Series,
) -> dict[tuple[str, str, str], float]:
    """Aggregate per-container ``Created`` events across iterations.

    Returns ``{(namespace, pod_basename, container): median_t_created_offset_s}``
    relative to T1. Empty dict if the column is absent (older CSVs).
    """
    if "node_container_creates_json" not in ok.columns:
        return {}
    import json as _json
    from datetime import datetime as _dt
    t1_series = pd.to_datetime(t1_off_per_row, utc=True, errors="coerce")
    bucket: dict[tuple[str, str, str], list[float]] = {}
    _basename = _pod_basename
    for raw, t1 in zip(ok["node_container_creates_json"], t1_series):
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        if pd.isna(t1):
            continue
        try:
            entries = _json.loads(raw)
        except Exception:
            continue
        for e in entries:
            ts = e.get("t_created")
            if not ts:
                continue
            try:
                ts_t = _dt.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            ns = e.get("namespace") or ""
            pod = e.get("pod") or ""
            container = e.get("container") or ""
            if not (ns and pod and container):
                continue
            base = _basename(pod)
            s_off = (ts_t - t1.to_pydatetime()).total_seconds()
            bucket.setdefault((ns, base, container), []).append(s_off)
    return {k: float(pd.Series(v).median()) for k, v in bucket.items()}



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


def _node_image_pulls_aggregated(ok: pd.DataFrame, t1_off_per_row: pd.Series) -> list[dict]:
    """Aggregate per-iteration `node_image_pulls_json` payloads into a list of
    per-image rows (image-centric), each carrying T1-relative offsets.

    Within a single iteration, kubelet emits one `Pulling`/`Pulled` event per
    POD referencing an image (e.g. csi-node-driver-registrar shows up twice
    because both csi-azuredisk-node and csi-azurefile-node use it), but
    containerd only pulls the image once and serves the rest from local
    cache. We dedupe by (iteration, image) keeping the instance with the
    largest `duration_s` — i.e. the real cold pull — so cache-hit
    duplicates don't multi-count or visually clutter the plots.

    Returns one dict per (image, iteration) pair after dedup:
      {image, family, start_off, end_off, duration_s, iteration_idx,
       cache_hits: int}  # cache_hits = number of cache-hit instances dropped
    """
    if "node_image_pulls_json" not in ok.columns:
        return []
    import json as _json
    from datetime import datetime as _dt
    from .image_family import classify
    out: list[dict] = []
    t1_series = pd.to_datetime(ok.get("T1_node_registered"), errors="coerce", utc=True)
    for (idx, raw), t1_ts in zip(ok["node_image_pulls_json"].items(), t1_series):
        if not isinstance(raw, str) or not raw:
            continue
        if pd.isna(t1_ts):
            continue
        try:
            entries = _json.loads(raw)
        except Exception:
            continue
        t1_epoch = t1_ts.timestamp()
        # Group instances of the same image within this iteration.
        by_image: dict[str, list[dict]] = {}
        for e in entries:
            tpl = e.get("t_pulling")
            tpd = e.get("t_pulled")
            if not tpd:
                # No "Pulled" event at all: nothing to anchor a window
                # to. Skip — these are typically failed/no-op entries.
                continue
            try:
                e_t = _dt.fromisoformat(tpd.replace("Z", "+00:00"))
            except Exception:
                continue
            # Cache hits emit "Pulled" with no preceding "Pulling" event
            # (kubelet message: "Container image ... already present on
            # machine"). Surface them as zero-duration markers at the
            # Pulled timestamp so the reader can still see which images
            # were referenced even when no network pull happened.
            cache_hit = not tpl
            if tpl:
                try:
                    s_t = _dt.fromisoformat(tpl.replace("Z", "+00:00"))
                except Exception:
                    s_t = e_t
            else:
                s_t = e_t
            s_off = s_t.timestamp() - t1_epoch
            e_off = e_t.timestamp() - t1_epoch
            dur = e.get("duration_s")
            if not isinstance(dur, (int, float)):
                dur = 0.0 if cache_hit else None
            # Correct an inflated end-of-pull window: when N init
            # containers share an image, kubelet emits one Pulled event
            # per container (first = real download, rest = cache-hit
            # references). Our collector groups by (pod, image) and
            # keeps max(Pulled), so t_pulled drifts to the LAST init's
            # start — visually overlapping subsequent run:<init> lanes
            # for an image that was actually ready much earlier.
            # When we have a real Pulling timestamp AND a parsed
            # duration_s from the kubelet's "Successfully pulled ... in
            # Xs" message, prefer the derived end. This shrinks the
            # window back to the actual download time.
            if (not cache_hit
                    and tpl
                    and isinstance(dur, (int, float))
                    and dur >= 0):
                derived_end_off = s_off + float(dur)
                if derived_end_off < e_off:
                    e_off = derived_end_off
            by_image.setdefault(e.get("image") or "", []).append({
                "image": e.get("image") or "",
                "pod": e.get("pod") or "",
                "namespace": e.get("namespace") or "",
                "container": e.get("container") or "",
                "family": classify(e.get("image") or "") if (e.get("image")) else (e.get("family") or "other"),
                "start_off": s_off,
                "end_off": e_off,
                "duration_s": dur,
                "cache_hit": cache_hit,
                "iteration_idx": idx,
            })
        # Pick the "real" pull per image: the entry with the largest
        # parseable duration. If none have a duration (all None), keep the
        # earliest by start_off — that's almost always the real one too.
        for image, instances in by_image.items():
            real = max(
                instances,
                key=lambda d: (
                    d["duration_s"] if isinstance(d["duration_s"], (int, float)) else -1.0,
                    -d["start_off"],
                ),
            )
            if not isinstance(real["duration_s"], (int, float)):
                # All instances were cache hits without explicit duration;
                # synthesize from the wall-clock window of the chosen one.
                real["duration_s"] = max(real["end_off"] - real["start_off"], 0.0)
            real["cache_hits"] = len(instances) - 1
            out.append(real)
    return out



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

    # ---- Panel D data: image-pull critical-path by family ----
    from .image_family import FAMILY_COLORS, DEFAULT_FAMILY
    # Per-family critical-path contribution per run: for each iteration of a
    # run, sum durations within each family, then take the median across
    # iterations. This is an approximation that ignores parallelism within
    # a family (multiple cilium init images on one node may pull in
    # parallel), but it's the right granularity for cross-provider
    # comparison of "which family costs how much to pull cold."
    def _family_breakdown(df: pd.DataFrame) -> tuple[dict[str, float], int, float]:
        """Returns (family_p50_seconds, distinct_image_p50, critical_p50).

        Dedupes per-pod cache-hit duplicates within each iteration by
        keeping only the maximum-duration pull per (image) — mirrors the
        single-run sub-panel logic.
        """
        if "node_image_pulls_json" not in df.columns:
            return {}, 0, float("nan")
        import json as _json
        from .image_family import classify as _classify
        per_iter_fam: list[dict[str, float]] = []
        per_iter_distinct: list[int] = []
        for raw in df["node_image_pulls_json"].dropna():
            if not isinstance(raw, str) or not raw:
                continue
            try:
                entries = _json.loads(raw)
            except Exception:
                continue
            # Dedup by image (keep max duration).
            best: dict[str, tuple[str, float]] = {}  # image -> (family, dur)
            for e in entries:
                img = e.get("image") or ""
                # Re-classify on read so stale CSV families don't survive.
                fam = _classify(img) if img else (e.get("family") or DEFAULT_FAMILY)
                d = e.get("duration_s")
                if not isinstance(d, (int, float)) or not img:
                    continue
                prev = best.get(img)
                if prev is None or float(d) > prev[1]:
                    best[img] = (fam, float(d))
            fam_sum: dict[str, float] = {}
            for _img, (fam, d) in best.items():
                fam_sum[fam] = fam_sum.get(fam, 0.0) + d
            per_iter_fam.append(fam_sum)
            per_iter_distinct.append(len(best))
        if not per_iter_fam:
            return {}, 0, float("nan")
        all_fams = sorted({f for d in per_iter_fam for f in d})
        fam_p50: dict[str, float] = {}
        for f in all_fams:
            vals = [d.get(f, 0.0) for d in per_iter_fam]
            fam_p50[f] = float(pd.Series(vals).median())
        distinct_p50 = int(pd.Series(per_iter_distinct).median())
        critical_p50 = _p(df, "image_pulls_critical_s", 0.5)
        return fam_p50, distinct_p50, critical_p50

    rows_d: list[tuple[str, dict[str, float], int, float]] = []
    for label, df in runs:
        fam_p50, distinct, crit = _family_breakdown(df)
        if fam_p50:
            rows_d.append((label, fam_p50, distinct, crit))
    # Family ordering across the panel — by total contribution descending.
    d_family_order: list[str] = []
    if rows_d:
        totals: dict[str, float] = {}
        for _l, fam_p50, _d, _c in rows_d:
            for f, v in fam_p50.items():
                totals[f] = totals.get(f, 0.0) + v
        d_family_order = sorted(totals.keys(), key=lambda f: -totals[f])
    has_panel_d = bool(rows_d)

    # ---- Figure layout ----
    n = len(rows_a)
    h_a = max(2.2, 0.45 * n + 1.5)
    h_b = max(2.0, 0.40 * len(rows_b) + 1.2)
    h_c = max(2.0, 0.50 * len(rows_c) + 1.0)
    h_d = max(2.0, 0.50 * len(rows_d) + 1.0) if has_panel_d else 0.0
    if has_panel_d:
        fig, axes = plt.subplots(
            4, 1, figsize=(16, h_a + h_b + h_c + h_d),
            gridspec_kw={"height_ratios": [h_a, h_b, h_c, h_d]},
        )
        ax_a, ax_b, ax_c, ax_d = axes
    else:
        fig, axes = plt.subplots(
            3, 1, figsize=(16, h_a + h_b + h_c),
            gridspec_kw={"height_ratios": [h_a, h_b, h_c]},
        )
        ax_a, ax_b, ax_c = axes
        ax_d = None

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

    # --- Panel D: image-pull critical-path by family ---
    if ax_d is not None and rows_d:
        # Sort by image_pulls_critical_s p50 ascending (fastest at top).
        rows_d_sorted = sorted(
            rows_d,
            key=lambda r: r[3] if np.isfinite(r[3]) else float("inf"),
        )
        yd = np.arange(len(rows_d_sorted))
        cursor_d = np.zeros(len(rows_d_sorted))
        for fam in d_family_order:
            widths = np.array([r[1].get(fam, 0.0) for r in rows_d_sorted])
            if widths.sum() <= 0:
                continue
            color = FAMILY_COLORS.get(fam, FAMILY_COLORS[DEFAULT_FAMILY])
            ax_d.barh(yd, widths, left=cursor_d, color=color,
                      edgecolor="white", linewidth=0.5, label=fam)
            for j, w in enumerate(widths):
                if w >= 0.8:
                    ax_d.text(cursor_d[j] + w / 2, yd[j], f"{w:.1f}",
                              ha="center", va="center", fontsize=7,
                              color="black", fontweight="bold")
            cursor_d += widths
        # Annotations: distinct image count + critical-path span.
        for j, (_lab, _fam, distinct, crit) in enumerate(rows_d_sorted):
            crit_str = f"{crit:.1f}s" if np.isfinite(crit) else "n/a"
            ax_d.text(cursor_d[j] + 0.4, yd[j],
                      f"{distinct} imgs  ·  critical-path {crit_str}",
                      va="center", fontsize=8, color="#333")
        ax_d.set_yticks(yd)
        ax_d.set_yticklabels([r[0] for r in rows_d_sorted], fontsize=9)
        ax_d.invert_yaxis()
        ax_d.set_xlabel("seconds (sum of per-family image-pull durations, p50)")
        ax_d.set_title(
            "Image-pull cost by family — distinct images pulled per node + critical-path window",
            fontsize=10, fontweight="bold",
        )
        ax_d.grid(True, axis="x", alpha=0.3)
        ax_d.legend(loc="center left", bbox_to_anchor=(1.02, 0.5),
                    fontsize=7, framealpha=0.95, title="family")

    fig.suptitle(
        fontsize=12, fontweight="bold", y=0.995,
    )
    fig.tight_layout(rect=(0, 0, 0.83, 0.985))
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "compare_phase_decomposition.png"
    fig.savefig(p, dpi=140); plt.close(fig)
    return p


# Pod-running breakdown segments (left → right within the stacked bar).
# Phase column → (legend label, ACTOR_COLORS key).
_POD_RUNNING_PHASES = [
    ("trigger_prepull_s",     "sandbox / CNI ADD",   "sandbox"),
    ("trigger_image_pull_s",  "image pull",          "image_pull"),
    ("trigger_create_s",      "container create",    "kubelet"),
    ("trigger_run_gap_s",     "container start",     "kubelet_main"),
]


def _plot_compare_pod_running(csvs: list[Path], out_dir: Path) -> Path | None:
    """Cross-provider decomposition of the [trigger pod scheduled → Running]
    window into prepull / image_pull / create / run_gap phases.

    Three panels (top → bottom):
      1. Stacked horizontal bars (p50 of each phase per provider), ordered
         by total time-to-running ascending. Single number to compare.
      2. Box plot of ``trigger_total_s`` per provider — shows distribution.
      3. CDF of ``trigger_total_s`` per provider — same as compare_cdf.png
         but for the pod-running KPI rather than node-ready.
    """
    from .analysis import enrich_trigger_pod_metrics

    runs: list[tuple[str, pd.DataFrame]] = []
    for csv in csvs:
        try:
            df = pd.read_csv(csv)
        except Exception:
            continue
        df = enrich_trigger_pod_metrics(df)
        df = _ok(df)
        if df.empty:
            continue
        if pd.to_numeric(df.get("trigger_total_s"), errors="coerce").dropna().empty:
            continue
        runs.append((_run_label(csv), df))
    if not runs:
        return None

    def _p50(df: pd.DataFrame, col: str) -> float:
        s = pd.to_numeric(df.get(col), errors="coerce").dropna()
        return float(s.quantile(0.50)) if not s.empty else 0.0

    # Sort by p50 total ascending so the fastest provider sits on top.
    labels_totals = [(lbl, df, _p50(df, "trigger_total_s")) for lbl, df in runs]
    labels_totals.sort(key=lambda x: x[2])
    labels = [x[0] for x in labels_totals]
    dfs = [x[1] for x in labels_totals]
    totals = [x[2] for x in labels_totals]

    n = len(labels)
    fig, axes = plt.subplots(
        3, 1,
        figsize=(11, max(5.5, 0.5 * n + 6.5)),
        gridspec_kw={"height_ratios": [max(2.0, 0.45 * n + 1.0), 3.0, 3.0]},
    )
    ax_bar, ax_box, ax_cdf = axes

    # ---- Panel 1: stacked horizontal bars (p50 per phase) ----
    y = np.arange(n)
    left = np.zeros(n)
    for col, legend, color_key in _POD_RUNNING_PHASES:
        widths = np.array([_p50(df, col) for df in dfs])
        if not widths.any():
            continue
        ax_bar.barh(y, widths, left=left, height=0.62,
                    color=ACTOR_COLORS.get(color_key, "#cbd5e1"),
                    edgecolor="white", label=legend)
        left = left + widths
    ax_bar.set_yticks(y)
    ax_bar.set_yticklabels(labels, fontsize=9)
    ax_bar.invert_yaxis()  # fastest provider at the top
    ax_bar.set_xlabel("seconds (p50 of each phase)")
    ax_bar.set_title(
        "Pod-running decomposition by provider — "
        "p50 trigger pod: scheduled → Running"
    )
    ax_bar.grid(True, axis="x", alpha=0.3)
    ax_bar.legend(loc="lower right", fontsize=8, framealpha=0.95, ncol=2)
    # Annotate total at the end of each bar.
    for i, t in enumerate(totals):
        ax_bar.text(left[i], y[i], f"  {t:.2f}s", va="center",
                    fontsize=8, color="#374151")

    # ---- Panel 2: box plot of trigger_total_s ----
    box_data = [pd.to_numeric(df.get("trigger_total_s"), errors="coerce").dropna().values
                for df in dfs]
    box_pos = np.arange(1, n + 1)
    bp = ax_box.boxplot(
        box_data, positions=box_pos, orientation="horizontal", widths=0.55,
        patch_artist=True, showfliers=True,
    )
    for patch in bp["boxes"]:
        patch.set_facecolor(ACTOR_COLORS["trigger_pod"])
        patch.set_alpha(0.55)
        patch.set_edgecolor("#581c87")
    for med in bp["medians"]:
        med.set_color("#1f2937"); med.set_linewidth(1.4)
    ax_box.set_yticks(box_pos)
    ax_box.set_yticklabels(labels, fontsize=9)
    ax_box.invert_yaxis()
    ax_box.set_xlabel("trigger_total_s (sandbox_setup_s) — distribution")
    ax_box.set_title("Per-iteration spread of the pod-running window")
    ax_box.grid(True, axis="x", alpha=0.3)

    # ---- Panel 3: CDF overlay of trigger_total_s ----
    for lbl, df in zip(labels, dfs):
        s = pd.to_numeric(df.get("trigger_total_s"), errors="coerce").dropna().sort_values()
        if s.empty:
            continue
        cdf = np.arange(1, len(s) + 1) / len(s)
        ax_cdf.plot(s.values, cdf, marker=".", label=lbl)
    ax_cdf.set_xlabel("trigger_total_s (s)")
    ax_cdf.set_ylabel("CDF")
    ax_cdf.set_title("trigger_total_s CDF — provider comparison")
    ax_cdf.grid(True, alpha=0.3)
    ax_cdf.legend(fontsize=8, loc="lower right")

    fig.suptitle(
        "Trigger pod lifecycle — cross-provider comparison",
        fontsize=12, fontweight="bold", y=0.998,
    )
    fig.tight_layout(rect=(0, 0, 1.0, 0.985))
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "compare_pod_running.png"
    fig.savefig(p, dpi=140); plt.close(fig)
    return p


def plot_compare(csvs: list[Path], out_dir: Path) -> list[Path]:
    """Overlay headline-latency CDF across multiple runs and emit a
    cross-provider phase decomposition figure (cross-provider compare)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 5))
    cdf_metric = "time_to_runnable_s"
    # Filter out missing/unreadable iterations.csv up-front so the rest of the
    # pipeline doesn't have to defend against them. An incomplete run dir
    # (cluster create succeeded but every iteration failed before flush) won't
    # have iterations.csv on disk — skip with a warning, don't crash.
    valid_csvs: list[Path] = []
    for csv in csvs:
        if not csv.exists():
            print(f"  warn: skipping {csv.parent.name} (no iterations.csv)")
            continue
        valid_csvs.append(csv)
    if not valid_csvs:
        print("  warn: no valid run dirs to compare; nothing emitted")
        plt.close(fig)
        return []
    csvs = valid_csvs
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
        try:
            df = pd.read_csv(csv)
        except Exception as e:
            print(f"  warn: skipping {csv.parent.name} (read error: {e})")
            continue
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
    pod_running = _plot_compare_pod_running(csvs, out_dir)
    if pod_running is not None:
        out.append(pod_running)
    return out
