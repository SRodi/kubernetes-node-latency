"""Data records persisted across runs."""
from __future__ import annotations

import dataclasses as dc
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).isoformat() if ts else None


def delta(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


@dc.dataclass
class IterationRecord:
    iteration: int
    run_id: str
    provider: str
    region: str

    pod_name: str | None = None
    node_name: str | None = None

    T0_pod_created: datetime | None = None
    T1_node_registered: datetime | None = None
    T2_cilium_started: datetime | None = None
    T3_cilium_ready: datetime | None = None
    T4_node_ready: datetime | None = None
    # First moment the node was both Ready=True and free of CNI-applied
    # NoSchedule taints (e.g. `node.cilium.io/agent-not-ready` on AKS
    # managed Cilium). Equals T4 on platforms where no such taint is
    # configured (GKE DPv2, AKS kubenet).
    T4b_schedulable: datetime | None = None
    # First moment the kubelet's Ready=False condition stopped reporting
    # `NetworkPluginNotReady / cni config uninitialized` — i.e. the CNI
    # plugin dropped its conflist into /etc/cni/net.d/. This is the *only*
    # CNI-side signal that blocks Node Ready on a vanilla kubelet, so the
    # window T1c − T1 is the real CNI-induced Node-Ready delay.
    T1c_cni_conflist: datetime | None = None

    # T1\u2192T1c internal decomposition — populated by Collector enrichment.
    # First time the kubelet's Ready=False message stopped reporting
    # `CSINode is not yet initialized`. Independent of the CNI phrase,
    # captured from the same watch loop. None if never observed.
    T_csinode_ready: datetime | None = None
    # First time we observed any of the probe's blocking taints on the new
    # node (e.g. `node.cilium.io/agent-not-ready`). Proxy for when
    # cilium-operator stamped the taint.
    T_taint_observed: datetime | None = None
    # Cilium DS pod scheduling / image-pull / init-container timings,
    # populated by `Collector.collect_pod_lifecycle` after T3.
    T_pod_scheduled: datetime | None = None
    T_pod_initialized: datetime | None = None
    T_image_pull_start: datetime | None = None
    T_image_pulled: datetime | None = None
    # Per init container: [{"name": str, "started_at": iso|None,
    #                       "finished_at": iso|None}, ...]
    init_containers: list[dict] | None = None

    # Trigger-pod lifecycle (the workload pod the harness submits each
    # iteration to force a new node). Captured post-iteration so we can
    # measure the actual "K8s networking" cost a workload sees: from node
    # registered (T1) to the trigger pod's first container in Running.
    # `T_trigger_scheduled` is pod.status.conditions[PodScheduled]; it can
    # legitimately precede `T1c_cni_conflist` because the scheduler binds
    # purely on Node Ready and does not gate on CNI conflist presence.
    # `T5_pod_running` is the first container's
    # status.state.running.startedAt — the moment kubelet successfully
    # invoked CNI ADD and the pod sandbox is wired with an IP.
    T_trigger_scheduled: datetime | None = None
    T5_pod_running: datetime | None = None

    status: str = "pending"  # pending|success|timeout|error
    error: str | None = None

    # Tier-1 deep-Cilium headline numbers (None when --deep-cilium not set
    # or when the agent pod was unreachable).
    deep_cilium: dict | None = None

    def to_row(self) -> dict:
        from .cilium_deep import headline_to_columns
        import json as _json
        # Serialize init_containers: list[dict] with datetime values \u2192 list[dict] iso strings.
        init_serial: str | None = None
        ic_durations: dict[str, float] = {}
        if self.init_containers:
            slim = []
            for ic in self.init_containers:
                name = ic.get("name") or ""
                sa = ic.get("started_at")
                fa = ic.get("finished_at")
                sa_iso = iso(sa) if isinstance(sa, datetime) else sa
                fa_iso = iso(fa) if isinstance(fa, datetime) else fa
                slim.append({"name": name, "started_at": sa_iso, "finished_at": fa_iso})
                if isinstance(sa, datetime) and isinstance(fa, datetime):
                    safe_name = name.replace("-", "_")
                    ic_durations[f"initc_{safe_name}_s"] = max(
                        (fa - sa).total_seconds(), 0.0)
            init_serial = _json.dumps(slim, separators=(",", ":"))
        row = {
            "iteration": self.iteration,
            "run_id": self.run_id,
            "provider": self.provider,
            "region": self.region,
            "node_name": self.node_name,
            "pod_name": self.pod_name,
            "T0_pod_created": iso(self.T0_pod_created),
            "T1_node_registered": iso(self.T1_node_registered),
            "T2_cilium_started": iso(self.T2_cilium_started),
            "T3_cilium_ready": iso(self.T3_cilium_ready),
            "T4_node_ready": iso(self.T4_node_ready),
            "T4b_schedulable": iso(self.T4b_schedulable),
            "T1c_cni_conflist": iso(self.T1c_cni_conflist),
            "node_startup_latency_s": delta(self.T4_node_ready, self.T0_pod_created),
            "time_to_schedulable_s": delta(self.T4b_schedulable, self.T0_pod_created),
            "node_register_latency_s": delta(self.T1_node_registered, self.T0_pod_created),
            # Autoscaler-free counterpart to node_startup_latency_s: the time
            # from kubelet registering the Node (T1) to Node Ready=True (T4).
            # Equals cni_conflist_install_s + post_conflist_ready_s and
            # excludes the cloud-side autoscaler/VM-provisioning portion.
            "node_ready_after_register_s": (
                max(delta(self.T4_node_ready, self.T1_node_registered) or 0.0, 0.0)
                if (self.T4_node_ready and self.T1_node_registered) else None
            ),
            "cilium_init_duration_s": delta(self.T3_cilium_ready, self.T2_cilium_started),
            # CNI conflist placement is the only signal blocking Node Ready on
            # a vanilla kubelet. cni_conflist_install_s = T1c − T1 measures
            # how long kubelet sat with `NetworkPluginNotReady` after node
            # registration; post_conflist_ready_s = T4 − T1c is the residual
            # kubelet readiness work after the conflist landed.
            "cni_conflist_install_s": (
                max(delta(self.T1c_cni_conflist, self.T1_node_registered) or 0.0, 0.0)
                if (self.T1c_cni_conflist and self.T1_node_registered) else None
            ),
            "post_conflist_ready_s": (
                max(delta(self.T4_node_ready, self.T1c_cni_conflist) or 0.0, 0.0)
                if (self.T1c_cni_conflist and self.T4_node_ready) else None
            ),
            "cni_induced_delay_s": (
                max(delta(self.T4_node_ready, self.T3_cilium_ready) or 0.0, 0.0)
                if (self.T3_cilium_ready and self.T4_node_ready) else None
            ),
            # Directly-measured "Cilium taint blocks pod scheduling after Node
            # Ready=True" delay. Captures the AKS-style gating the legacy
            # `cni_induced_delay_s` misses (the operator stamps
            # `node.cilium.io/agent-not-ready:NoSchedule` and only the local
            # agent clears it at ~T3, so T4b > T4 even though T4 < T3).
            "cilium_scheduling_block_s": (
                max(delta(self.T4b_schedulable, self.T4_node_ready) or 0.0, 0.0)
                if (self.T4b_schedulable and self.T4_node_ready) else None
            ),
            "status": self.status,
            "error": self.error,
            # T1\u2192T1c decomposition columns
            "T_csinode_ready": iso(self.T_csinode_ready),
            "T_taint_observed": iso(self.T_taint_observed),
            "T_pod_scheduled": iso(self.T_pod_scheduled),
            "T_pod_initialized": iso(self.T_pod_initialized),
            "T_image_pull_start": iso(self.T_image_pull_start),
            "T_image_pulled": iso(self.T_image_pulled),
            "init_containers_json": init_serial,
            # Derived seconds relative to T1 (None when either anchor is missing).
            "csinode_block_s": (
                max(delta(self.T_csinode_ready, self.T1_node_registered) or 0.0, 0.0)
                if (self.T_csinode_ready and self.T1_node_registered) else None
            ),
            "taint_observed_offset_s": (
                delta(self.T_taint_observed, self.T1_node_registered)
                if (self.T_taint_observed and self.T1_node_registered) else None
            ),
            "pod_scheduling_lag_s": (
                max(delta(self.T_pod_scheduled, self.T1_node_registered) or 0.0, 0.0)
                if (self.T_pod_scheduled and self.T1_node_registered) else None
            ),
            "image_pull_s": (
                max(delta(self.T_image_pulled, self.T_image_pull_start) or 0.0, 0.0)
                if (self.T_image_pull_start and self.T_image_pulled) else None
            ),
            "image_pulled_offset_s": (
                delta(self.T_image_pulled, self.T1_node_registered)
                if (self.T_image_pulled and self.T1_node_registered) else None
            ),
            "pod_initialized_offset_s": (
                delta(self.T_pod_initialized, self.T1_node_registered)
                if (self.T_pod_initialized and self.T1_node_registered) else None
            ),
            # Trigger pod lifecycle (workload pod, not the cilium-agent DS).
            "T_trigger_scheduled": iso(self.T_trigger_scheduled),
            "T5_pod_running": iso(self.T5_pod_running),
            # K8s-networking headline KPI: T1 -> trigger pod's first
            # container Running. Excludes IaaS / VM-bringup noise (T0->T1)
            # and is the cleanest cross-provider measure of CNI + kubelet
            # wiring + cilium IPAM costs the workload actually pays.
            "time_to_runnable_s": (
                max(delta(self.T5_pod_running, self.T1_node_registered) or 0.0, 0.0)
                if (self.T5_pod_running and self.T1_node_registered) else None
            ),
            # CNI ADD + sandbox setup as observed by kubelet on the trigger
            # pod: from scheduler binding to first container Running. This
            # is the workload-pod analogue of `cilium_bootstrap_ipam_s`
            # (which is the *agent's* view) and reflects the real-world
            # cost of CNI ADD on the new node.
            "sandbox_setup_s": (
                max(delta(self.T5_pod_running, self.T_trigger_scheduled) or 0.0, 0.0)
                if (self.T5_pod_running and self.T_trigger_scheduled) else None
            ),
            # T1-anchored versions of the canonical markers. These are the
            # numbers to compare across providers when the focus is on
            # K8s networking rather than IaaS provisioning. All are None
            # when either anchor is missing.
            "T1c_s_from_T1": (
                delta(self.T1c_cni_conflist, self.T1_node_registered)
                if (self.T1c_cni_conflist and self.T1_node_registered) else None
            ),
            "T2_s_from_T1": (
                delta(self.T2_cilium_started, self.T1_node_registered)
                if (self.T2_cilium_started and self.T1_node_registered) else None
            ),
            "T3_s_from_T1": (
                delta(self.T3_cilium_ready, self.T1_node_registered)
                if (self.T3_cilium_ready and self.T1_node_registered) else None
            ),
            "T4_s_from_T1": (
                delta(self.T4_node_ready, self.T1_node_registered)
                if (self.T4_node_ready and self.T1_node_registered) else None
            ),
            "T4b_s_from_T1": (
                delta(self.T4b_schedulable, self.T1_node_registered)
                if (self.T4b_schedulable and self.T1_node_registered) else None
            ),
            "T5_s_from_T1": (
                delta(self.T5_pod_running, self.T1_node_registered)
                if (self.T5_pod_running and self.T1_node_registered) else None
            ),
        }
        row.update(ic_durations)
        row.update(headline_to_columns(self.deep_cilium))
        return row
