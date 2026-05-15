"""K8s watchers + log scanners that capture T0-T4 for one iteration."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from kubernetes import client, watch

from .cni.base import CNIProbe
from .records import IterationRecord, utcnow

log = logging.getLogger(__name__)


def _parse_k8s_time(ts) -> datetime | None:
    """Normalize various K8s timestamp formats to aware UTC."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts).replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)


# Cilium agent log lines are typically: "level=info ts=2025-01-01T12:34:56.789Z msg=..."
_LOG_TS_RE = re.compile(
    r'(?:ts=|time=")?(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))'
)


def _extract_log_ts(line: str) -> datetime | None:
    m = _LOG_TS_RE.search(line)
    if not m:
        return None
    try:
        return _parse_k8s_time(m.group("ts"))
    except Exception:
        return None


class EventSink:
    """Append-only JSONL writer of every notable event for offline replay."""

    def __init__(self, path: Path):
        self.path = path
        self._fp = open(path, "a", buffering=1)

    def write(self, kind: str, obj: dict) -> None:
        self._fp.write(json.dumps({"kind": kind, "ts": utcnow().isoformat(), **obj}) + "\n")

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass


def _container_started_at(pod: client.V1Pod, container_name: str) -> datetime | None:
    """Best-effort T2: the earliest time we can attribute to the agent container starting.

    Search order:
      1. containerStatuses[name].state.running.startedAt
      2. containerStatuses[name].lastState.running.startedAt
      3. containerStatuses[name].state.terminated.startedAt
      4. any other container in containerStatuses with state.running.startedAt
      5. latest initContainerStatus terminated.finishedAt (init phase complete)
      6. pod.status.startTime
    """
    css = pod.status.container_statuses or []
    by_name = {cs.name: cs for cs in css}
    cs = by_name.get(container_name)
    if cs is not None:
        if cs.state and cs.state.running and cs.state.running.started_at:
            return _parse_k8s_time(cs.state.running.started_at)
        if cs.last_state and cs.last_state.running and cs.last_state.running.started_at:
            return _parse_k8s_time(cs.last_state.running.started_at)
        if cs.state and cs.state.terminated and cs.state.terminated.started_at:
            return _parse_k8s_time(cs.state.terminated.started_at)
    for other in css:
        if other.state and other.state.running and other.state.running.started_at:
            return _parse_k8s_time(other.state.running.started_at)
    finished = []
    for ic in (pod.status.init_container_statuses or []):
        if ic.state and ic.state.terminated and ic.state.terminated.finished_at:
            ts = _parse_k8s_time(ic.state.terminated.finished_at)
            if ts:
                finished.append(ts)
    if finished:
        return max(finished)
    return _parse_k8s_time(pod.status.start_time)


def _pod_ready_transition(pod: client.V1Pod, condition_type: str) -> datetime | None:
    for c in (pod.status.conditions or []):
        if c.type == condition_type and c.status == "True" and c.last_transition_time:
            return _parse_k8s_time(c.last_transition_time)
    return None


class Collector:
    """Coordinates timestamp capture for a single iteration."""

    def __init__(self, core: client.CoreV1Api, probe: CNIProbe, sink: EventSink):
        self.core = core
        self.probe = probe
        self.sink = sink

    # ----- T1: first time the new node shows up in the API -----
    def wait_for_new_node(self, before_nodes: set[str], timeout_s: int,
                          *, not_before: datetime | None = None,
                          skew_tolerance_s: float = 2.0) -> tuple[str, datetime]:
        """Wait for a node whose creationTimestamp is at or after ``not_before``.

        Nodes that pre-date the trigger (with `skew_tolerance_s` of clock skew)
        are skipped: they were already being provisioned for unrelated reasons
        and would yield bogus negative latencies.

        Resilient to the apiserver closing watch streams early (it typically
        does so well before our budget), by re-opening the watch until the
        deadline is actually exceeded.
        """
        deadline = time.monotonic() + timeout_s
        cutoff = (not_before - timedelta(seconds=skew_tolerance_s)) if not_before else None
        # Client-side HTTP read timeout for each watch invocation. AKS/GKE
        # apiservers occasionally drop watch TCP streams without sending FIN,
        # which leaves `w.stream()` blocked on a socket read indefinitely and
        # the outer deadline never gets a chance to fire. Capping the read
        # forces the inner loop to exit so we can re-open the watch.
        read_timeout = min(120, max(30, timeout_s // 4))
        while time.monotonic() < deadline:
            remaining = max(1, int(deadline - time.monotonic()))
            w = watch.Watch()
            try:
                for ev in w.stream(self.core.list_node,
                                    timeout_seconds=remaining,
                                    _request_timeout=(10, read_timeout)):
                    node = ev["object"]
                    name = node.metadata.name
                    if name in before_nodes:
                        continue
                    ts = _parse_k8s_time(node.metadata.creation_timestamp) or utcnow()
                    if cutoff is not None and ts < cutoff:
                        self.sink.write("node_pre_dates_trigger",
                                        {"name": name, "creationTimestamp": ts.isoformat(),
                                         "not_before": not_before.isoformat()})
                        before_nodes.add(name)
                        continue
                    self.sink.write("node_added", {"name": name, "creationTimestamp": ts.isoformat()})
                    return name, ts
            except Exception as e:  # noqa: BLE001
                # ReadTimeoutError / ProtocolError / chunked-encoding errors:
                # treat as a transient watch drop and re-open. Log once per
                # occurrence so silent hangs are visible in raw_events.
                self.sink.write("node_watch_reopen", {"error": type(e).__name__,
                                                       "msg": str(e)[:200]})
            finally:
                w.stop()
            # apiserver closed the stream early; loop and re-watch with remaining budget.
        raise TimeoutError("no genuinely-new node observed before timeout")

    # ----- T1c / T4 / T4b: Ready=True transition + first moment node is schedulable -----
    def wait_for_node_ready(
        self, node_name: str, timeout_s: int
    ) -> tuple[datetime, datetime, datetime | None, datetime | None, datetime | None]:
        """Wait for the node to become both Ready=True AND schedulable.

        Returns ``(T4_node_ready, T4b_schedulable, T1c_cni_conflist,
        T_csinode_ready, T_taint_observed)``.

        * T4_node_ready: ``lastTransitionTime`` of the Ready condition's
          ``True`` transition.
        * T4b_schedulable: first watch event after T4 at which none of
          ``self.probe.blocking_taint_keys`` are present in
          ``node.spec.taints``. Equals T4 when no blocking taints are
          configured.
        * T1c_cni_conflist: first watch event at which the kubelet's
          ``"cni plugin not initialized"`` / ``NetworkPluginNotReady``
          phrase clears from the Ready condition message.
        * T_csinode_ready: first watch event at which the kubelet's
          ``"CSINode is not yet initialized"`` phrase clears. Independent
          of T1c — sometimes the real blocker on a CSI-heavy cloud.
        * T_taint_observed: first watch event at which any of the
          configured blocking taints (e.g. ``node.cilium.io/agent-not-ready``)
          was seen on the node. Useful for distinguishing
          cilium-operator-stamping latency from cilium-agent-clearing latency.
        """
        deadline = time.monotonic() + timeout_s
        read_timeout = min(120, max(30, timeout_s // 4))
        blocking = set(self.probe.blocking_taint_keys or ())
        t4: datetime | None = None
        t4b: datetime | None = None
        t1c: datetime | None = None
        t_csinode: datetime | None = None
        t_taint_observed: datetime | None = None
        saw_taint = False
        saw_cni_block = False
        saw_csinode_block = False
        while time.monotonic() < deadline:
            remaining = max(1, int(deadline - time.monotonic()))
            w = watch.Watch()
            try:
                for ev in w.stream(self.core.list_node,
                                   field_selector=f"metadata.name={node_name}",
                                   timeout_seconds=remaining,
                                   _request_timeout=(10, read_timeout)):
                    node = ev["object"]
                    ready_cond = None
                    for cond in (node.status.conditions or []):
                        if cond.type == "Ready":
                            ready_cond = cond
                            break
                    msg = (getattr(ready_cond, "message", "") or "") if ready_cond else ""
                    not_ready = ready_cond is not None and ready_cond.status != "True"
                    # --- T1c: CNI-blocking phrase clears ---
                    if t1c is None and ready_cond is not None:
                        cni_blocking = (
                            not_ready
                            and ("cni config uninitialized" in msg
                                 or "NetworkPluginNotReady" in msg)
                        )
                        if cni_blocking:
                            if not saw_cni_block:
                                saw_cni_block = True
                                self.sink.write("cni_conflist_blocking",
                                                {"name": node_name,
                                                 "message": msg[:200]})
                        elif saw_cni_block:
                            t1c = utcnow()
                            self.sink.write("cni_conflist_observed",
                                            {"name": node_name,
                                             "ts": t1c.isoformat()})
                    # --- T_csinode_ready: CSINode phrase clears ---
                    # Independent of CNI: kubelet composes Ready=False message
                    # from all unmet preconditions; "CSINode is not yet
                    # initialized" can clear before or after the CNI phrase.
                    if t_csinode is None and ready_cond is not None:
                        csi_blocking = not_ready and "CSINode is not yet initialized" in msg
                        if csi_blocking:
                            if not saw_csinode_block:
                                saw_csinode_block = True
                                self.sink.write("csinode_blocking",
                                                {"name": node_name,
                                                 "message": msg[:200]})
                        elif saw_csinode_block:
                            t_csinode = utcnow()
                            self.sink.write("csinode_ready",
                                            {"name": node_name,
                                             "ts": t_csinode.isoformat()})
                    if t4 is None and ready_cond is not None and ready_cond.status == "True":
                        t4 = _parse_k8s_time(ready_cond.last_transition_time) or utcnow()
                        self.sink.write("node_ready",
                                        {"name": node_name, "ts": t4.isoformat()})
                    if blocking:
                        present = {t.key for t in (node.spec.taints or [])} & blocking
                        if present:
                            if not saw_taint:
                                t_taint_observed = utcnow()
                                self.sink.write("node_blocking_taint_first_seen",
                                                {"name": node_name,
                                                 "ts": t_taint_observed.isoformat(),
                                                 "taints": sorted(present)})
                            saw_taint = True
                            self.sink.write("node_blocking_taint_present",
                                            {"name": node_name, "taints": sorted(present)})
                        elif t4 is not None and t4b is None:
                            if saw_taint:
                                t4b = utcnow()
                                self.sink.write("node_blocking_taint_cleared",
                                                {"name": node_name, "ts": t4b.isoformat(),
                                                 "keys": sorted(blocking)})
                            else:
                                t4b = t4
                                self.sink.write("node_blocking_taint_absent",
                                                {"name": node_name,
                                                 "keys": sorted(blocking),
                                                 "note": "never observed; T4b := T4"})
                    elif t4 is not None and t4b is None:
                        t4b = t4
                    if t4 is not None and t4b is not None:
                        return t4, t4b, t1c, t_csinode, t_taint_observed
            except Exception as e:  # noqa: BLE001
                self.sink.write("node_ready_watch_reopen",
                                {"node": node_name, "error": type(e).__name__,
                                 "msg": str(e)[:200]})
            finally:
                w.stop()
        if t4 is None:
            raise TimeoutError(f"node {node_name} did not become Ready before timeout")
        self.sink.write("node_schedulable_timeout",
                        {"node": node_name, "keys": sorted(blocking),
                         "note": "blocking taint still present at deadline; T4b := T4"})
        return t4, t4, t1c, t_csinode, t_taint_observed

    # ----- T2/T3: CNI agent — pod-watch driven, no log access required -----
    def find_agent_pod(self, node_name: str, timeout_s: int = 120) -> client.V1Pod | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            pods = self.core.list_namespaced_pod(
                self.probe.namespace, label_selector=self.probe.label_selector,
                field_selector=f"spec.nodeName={node_name}",
            ).items
            if pods:
                return pods[0]
            time.sleep(2)
        return None

    def watch_agent_pod(self, pod_name: str, timeout_s: int) -> tuple[datetime | None, datetime | None]:
        """Watch a single agent pod; return (T2_started_at, T3_pod_ready_at).

        T2 is captured the first time we observe a non-null startedAt.
        T3 is captured the first time the configured pod condition flips to True.
        """
        t2: datetime | None = None
        t3: datetime | None = None
        read_timeout = min(120, max(30, timeout_s // 4))
        w = watch.Watch()
        try:
            for ev in w.stream(
                self.core.list_namespaced_pod, namespace=self.probe.namespace,
                field_selector=f"metadata.name={pod_name}", timeout_seconds=timeout_s,
                _request_timeout=(10, read_timeout),
            ):
                pod = ev["object"]
                if t2 is None:
                    t2 = _container_started_at(pod, self.probe.container_name)
                    if t2:
                        self.sink.write("cilium_started", {"pod": pod_name, "ts": t2.isoformat()})
                if t3 is None:
                    t3 = _pod_ready_transition(pod, self.probe.pod_ready_condition)
                    if t3:
                        self.sink.write("cilium_ready", {"pod": pod_name, "ts": t3.isoformat(),
                                                          "source": "pod_condition"})
                if t2 and t3:
                    break
        finally:
            w.stop()
        # One last sync read in case the watch missed early state.
        if t2 is None or t3 is None:
            try:
                pod = self.core.read_namespaced_pod(name=pod_name, namespace=self.probe.namespace)
                t2 = t2 or _container_started_at(pod, self.probe.container_name)
                t3 = t3 or _pod_ready_transition(pod, self.probe.pod_ready_condition)
            except client.ApiException as e:
                self.sink.write("agent_pod_read_error", {"pod": pod_name, "error": str(e)})
        return t2, t3

    def collect_pod_lifecycle(self, pod_name: str, namespace: str) -> dict:
        """Post-T3 enrichment: re-read the cilium DS pod and pull events
        to derive scheduler latency, image-pull window, and per-init-container
        timing inside the T1\u2192T1c window.

        Returns a dict with optional keys (any may be absent):
            T_pod_scheduled        datetime
            T_pod_initialized      datetime  (pod.status.conditions[Initialized])
            T_image_pull_start     datetime  (event reason=Pulling)
            T_image_pulled         datetime  (event reason=Pulled)
            init_containers        list[{name, started_at, finished_at}]

        Best-effort: never raises; failures are logged to the sink.
        """
        out: dict = {"init_containers": []}
        try:
            pod = self.core.read_namespaced_pod(name=pod_name, namespace=namespace)
        except Exception as e:  # noqa: BLE001
            self.sink.write("pod_lifecycle_read_error",
                            {"pod": pod_name, "error": str(e)[:200]})
            return out
        for c in (pod.status.conditions or []):
            if c.type == "PodScheduled" and c.status == "True" and "T_pod_scheduled" not in out:
                ts = _parse_k8s_time(c.last_transition_time)
                if ts:
                    out["T_pod_scheduled"] = ts
            elif c.type == "Initialized" and c.status == "True" and "T_pod_initialized" not in out:
                ts = _parse_k8s_time(c.last_transition_time)
                if ts:
                    out["T_pod_initialized"] = ts
        for ic in (pod.status.init_container_statuses or []):
            entry: dict = {"name": ic.name}
            term = (ic.state and ic.state.terminated) or (ic.last_state and ic.last_state.terminated)
            if term:
                if term.started_at:
                    entry["started_at"] = _parse_k8s_time(term.started_at)
                if term.finished_at:
                    entry["finished_at"] = _parse_k8s_time(term.finished_at)
            out["init_containers"].append(entry)
        try:
            events = self.core.list_namespaced_event(
                namespace=namespace,
                field_selector=f"involvedObject.name={pod_name}",
            ).items
        except Exception as e:  # noqa: BLE001
            self.sink.write("pod_lifecycle_events_error",
                            {"pod": pod_name, "error": str(e)[:200]})
            events = []
        for ev in events:
            ts = _parse_k8s_time(
                getattr(ev, "first_timestamp", None)
                or getattr(ev, "event_time", None)
                or getattr(ev, "last_timestamp", None)
            )
            if not ts:
                continue
            reason = getattr(ev, "reason", "") or ""
            if reason == "Pulling" and "T_image_pull_start" not in out:
                out["T_image_pull_start"] = ts
            elif reason == "Pulled" and "T_image_pulled" not in out:
                out["T_image_pulled"] = ts
        self.sink.write("pod_lifecycle_collected",
                        {"pod": pod_name,
                         "init_count": len(out["init_containers"]),
                         "have_pull_start": "T_image_pull_start" in out,
                         "have_pulled": "T_image_pulled" in out,
                         "have_scheduled": "T_pod_scheduled" in out})
        return out


    def t3_ready_from_logs(self, pod: client.V1Pod, tail_lines: int,
                           *, retries: int = 6) -> datetime | None:
        """Fallback T3: scan agent logs for ready_regex with exponential backoff."""
        pat = self.probe.ready_pattern()
        delay = 2.0
        last_err: str | None = None
        for attempt in range(1, retries + 1):
            try:
                logs: str = self.core.read_namespaced_pod_log(
                    name=pod.metadata.name,
                    namespace=pod.metadata.namespace,
                    container=self.probe.container_name,
                    tail_lines=tail_lines,
                    timestamps=True,
                )
            except client.ApiException as e:
                last_err = str(e)
                self.sink.write("log_fetch_retry", {"pod": pod.metadata.name,
                                                     "attempt": attempt, "error": last_err[:200]})
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            for line in logs.splitlines():
                if pat.search(line):
                    head = line.split(" ", 1)[0]
                    ts = _extract_log_ts(head) or _extract_log_ts(line)
                    if ts:
                        self.sink.write("cilium_ready", {"pod": pod.metadata.name,
                                                          "ts": ts.isoformat(),
                                                          "source": "log_scan",
                                                          "line": line[:200]})
                        return ts
            return None  # logs fetched but no marker present yet
        if last_err:
            self.sink.write("log_fetch_error", {"pod": pod.metadata.name, "error": last_err[:200]})
        return None


def list_node_names(core: client.CoreV1Api) -> set[str]:
    return {n.metadata.name for n in core.list_node().items}


def cordon_nodes(core: client.CoreV1Api, names: Iterable[str], sink: EventSink | None = None) -> list[str]:
    """Mark nodes unschedulable so the scheduler can't reuse them. Returns names actually cordoned."""
    cordoned: list[str] = []
    body = {"spec": {"unschedulable": True}}
    for name in names:
        try:
            core.patch_node(name=name, body=body)
            cordoned.append(name)
        except client.ApiException as e:
            if sink:
                sink.write("cordon_error", {"node": name, "error": str(e)[:200]})
    if sink:
        sink.write("nodes_cordoned", {"nodes": cordoned})
    return cordoned


def uncordon_nodes(core: client.CoreV1Api, names: Iterable[str], sink: EventSink | None = None) -> None:
    """Best-effort uncordon; failures are logged but don't raise."""
    body = {"spec": {"unschedulable": False}}
    for name in names:
        try:
            core.patch_node(name=name, body=body)
        except client.ApiException as e:
            if sink:
                sink.write("uncordon_error", {"node": name, "error": str(e)[:200]})
