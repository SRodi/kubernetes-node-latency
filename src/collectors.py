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
    """Append-only JSONL writer of every notable event for offline replay.

    Usable as a context manager so the underlying file handle is released
    deterministically even when the runner crashes between sink creation and
    the iteration loop:

        with EventSink(path) as sink:
            sink.write(...)
    """

    def __init__(self, path: Path):
        self.path = path
        self._fp = open(path, "a", buffering=1)

    def write(self, kind: str, obj: dict) -> None:
        self._fp.write(json.dumps({"kind": kind, "ts": utcnow().isoformat(), **obj}) + "\n")

    def close(self) -> None:
        if self._fp is None:
            return
        fp, self._fp = self._fp, None  # type: ignore[assignment]
        try:
            fp.close()
        except OSError as e:
            # Disk full / read-only FS / broken pipe — caller deserves to know
            # raw_events.jsonl may be truncated.
            logging.getLogger(__name__).error(
                "EventSink close failed for %s: %s", self.path, e)
            raise

    def __enter__(self) -> "EventSink":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except OSError:
            # Already logged inside close(); don't mask the original exception.
            if exc_type is None:
                raise


def _container_started_at(pod: client.V1Pod, container_name: str) -> datetime | None:
    """Strict T2: the cilium-agent **main** container's current-run start.

    By k8s semantics the main container cannot transition to Running until
    every init container has terminated successfully, so T2 must always be
    >= max(init.finishedAt). Earlier iterations of this helper had
    fallbacks to (a) lastState.running.startedAt, (b) state.terminated,
    (c) any other container's startedAt, (d) pod.startTime — all of which
    can produce timestamps that pre-date init-end and yield an impossible
    T2 < last-init-finished (most visible on GKE where the init chain is
    short, so the misordering is obvious in plots).

    We now only accept `containerStatuses[<name>].state.running.startedAt`.
    When the main container isn't Running yet we return None and let the
    caller (a watch loop) re-read pod state until it is.
    """
    css = pod.status.container_statuses or []
    for cs in css:
        if cs.name != container_name:
            continue
        if cs.state and cs.state.running and cs.state.running.started_at:
            return _parse_k8s_time(cs.state.running.started_at)
        return None
    return None


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


    def collect_trigger_pod_status(self, pod_name: str, namespace: str) -> dict:
        """Capture the trigger (workload) pod's scheduled + running
        timestamps. Read at the end of an iteration, just before deletion,
        so we record kubelet's view of CNI ADD completion on a *workload*
        pod — distinct from the cilium-agent DS pod measured elsewhere.

        Returns a dict with optional keys (any may be absent):
            T_trigger_scheduled  datetime   pod.status.conditions[PodScheduled]
            T5_pod_running       datetime   first containerStatus state.running.startedAt
                                            (== "sandbox wired with an IP")

        Best-effort: never raises.
        """
        out: dict = {}
        try:
            pod = self.core.read_namespaced_pod(name=pod_name, namespace=namespace)
        except Exception as e:  # noqa: BLE001
            self.sink.write("trigger_pod_read_error",
                            {"pod": pod_name, "error": str(e)[:200]})
            return out
        for c in (pod.status.conditions or []):
            if c.type == "PodScheduled" and c.status == "True":
                ts = _parse_k8s_time(c.last_transition_time)
                if ts:
                    out["T_trigger_scheduled"] = ts
                break
        # First container Running startedAt — workload-side moment the
        # pod sandbox was wired up and the container could start.
        for cs in (pod.status.container_statuses or []):
            run = cs.state and cs.state.running
            if run and run.started_at:
                ts = _parse_k8s_time(run.started_at)
                if ts:
                    out["T5_pod_running"] = ts
                    break
        self.sink.write("trigger_pod_status_collected",
                        {"pod": pod_name,
                         "have_scheduled": "T_trigger_scheduled" in out,
                         "have_running": "T5_pod_running" in out,
                         "phase": getattr(pod.status, "phase", None)})
        return out


    def collect_node_image_pulls(
        self,
        node_name: str,
        window_start: datetime,
        window_end: datetime,
        *,
        trigger_pattern: str | None = None,
    ) -> list[dict]:
        """Capture every kubelet image pull observed on `node_name` within
        the time window [window_start, window_end].

        Strategy:
          1. List pods on the node (one API call) to scope event lookup
             and capture container -> image mappings.
          2. List `reason=Pulling` and `reason=Pulled` events cluster-wide
             (paginated); filter to pods on the target node and event
             timestamps inside the window.
          3. Pair Pulling/Pulled by (namespace, pod, image) using the
             image string parsed from the event message.
          4. Extract pull duration from the Pulled message; fall back to
             the wall-clock delta (Pulled - Pulling) when absent.
          5. Classify each image into a family via image_family.classify.

        Returns a list of dicts (possibly empty). Never raises; failures
        are logged to the sink.
        """
        from .image_family import classify, parse_pull_duration

        out: list[dict] = []
        # Pods on this node — used to (a) scope the event filter and
        # (b) resolve the container name for each (pod, image) pair.
        pod_index: dict[tuple[str, str], dict[str, str]] = {}
        try:
            pods = self.core.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={node_name}",
                _request_timeout=(10, 30),
            ).items
        except Exception as e:  # noqa: BLE001
            self.sink.write("node_image_pulls_pods_error",
                            {"node": node_name, "error": str(e)[:200]})
            return out
        for p in pods:
            ns = p.metadata.namespace
            name = p.metadata.name
            img_to_container: dict[str, str] = {}
            for c in (p.spec.init_containers or []):
                if c.image:
                    img_to_container.setdefault(c.image, c.name)
            for c in (p.spec.containers or []):
                if c.image:
                    img_to_container.setdefault(c.image, c.name)
            pod_index[(ns, name)] = img_to_container

        # Pull both reasons. Field-selector with reason= is cheap server-side.
        events: list = []
        for reason in ("Pulling", "Pulled", "Failed"):
            try:
                resp = self.core.list_event_for_all_namespaces(
                    field_selector=f"reason={reason}",
                    _request_timeout=(10, 30),
                )
                events.extend(resp.items or [])
            except Exception as e:  # noqa: BLE001
                self.sink.write("node_image_pulls_events_error",
                                {"reason": reason, "error": str(e)[:200]})
                continue

        # Group by (ns, pod, image) so we can pair Pulling -> Pulled.
        # message format: 'Pulling image "X"' / 'Successfully pulled image "X" in 5.234s ...'
        img_re = re.compile(r'image\s+"([^"]+)"', re.I)
        grouped: dict[tuple[str, str, str], dict] = {}
        for ev in events:
            io = getattr(ev, "involved_object", None)
            if io is None or (io.kind and io.kind != "Pod"):
                continue
            key_pod = (io.namespace, io.name)
            if key_pod not in pod_index:
                continue
            ts = _parse_k8s_time(
                getattr(ev, "event_time", None)
                or getattr(ev, "last_timestamp", None)
                or getattr(ev, "first_timestamp", None)
            )
            if not ts or ts < window_start or ts > window_end:
                continue
            msg = getattr(ev, "message", "") or ""
            m = img_re.search(msg)
            if not m:
                continue
            image = m.group(1)
            reason = getattr(ev, "reason", "") or ""
            key = (io.namespace, io.name, image)
            entry = grouped.setdefault(key, {
                "namespace": io.namespace,
                "pod": io.name,
                "image": image,
                "container": pod_index[key_pod].get(image),
                "t_pulling": None,
                "t_pulled": None,
                "duration_s": None,
                "failed": False,
            })
            if reason == "Pulling":
                if entry["t_pulling"] is None or ts < entry["t_pulling"]:
                    entry["t_pulling"] = ts
            elif reason == "Pulled":
                if entry["t_pulled"] is None or ts > entry["t_pulled"]:
                    entry["t_pulled"] = ts
                d = parse_pull_duration(msg)
                if d is not None:
                    entry["duration_s"] = d
            elif reason == "Failed":
                # Only flag failures whose message references image pull.
                if "pull" in msg.lower() or "ErrImagePull" in msg or "ImagePullBackOff" in msg:
                    entry["failed"] = True

        # Backfill duration_s from wall-clock delta when message parsing missed.
        for entry in grouped.values():
            if (entry["duration_s"] is None
                    and entry["t_pulling"] is not None
                    and entry["t_pulled"] is not None):
                entry["duration_s"] = max(
                    (entry["t_pulled"] - entry["t_pulling"]).total_seconds(), 0.0)
            entry["family"] = classify(entry["image"], extra_trigger_pattern=trigger_pattern)
            out.append(entry)

        out.sort(key=lambda e: (
            e["t_pulling"] or e["t_pulled"] or window_start,
            e["image"],
        ))
        self.sink.write("node_image_pulls_collected",
                        {"node": node_name,
                         "count": len(out),
                         "with_duration": sum(1 for e in out if e["duration_s"] is not None),
                         "failed": sum(1 for e in out if e["failed"])})
        return out

    def collect_pod_logs(
        self,
        node_name: str,
        dest_dir: Path,
        *,
        targets: list[tuple[str, str]],
        since_time: datetime | None = None,
        tail_lines: int = 5000,
    ) -> dict:
        """Capture logs from selected pods on `node_name` into `dest_dir`.

        `targets` is a list of (namespace, label_selector) pairs — for each,
        every pod on the node matching the selector has its logs written to
        `dest_dir/<namespace>__<pod>__<container>.log`. All containers
        (init + main) are captured. Best-effort; never raises.

        Returns a small summary dict: {pods, containers, bytes, errors}.
        """
        from pathlib import Path as _Path
        dest = _Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        summary = {"pods": 0, "containers": 0, "bytes": 0, "errors": 0}
        since_s: int | None = None
        if since_time is not None:
            delta = (utcnow() - since_time).total_seconds()
            if delta > 0:
                since_s = int(delta) + 5  # small cushion
        seen_pods: set[tuple[str, str]] = set()
        for ns, sel in targets:
            try:
                pods = self.core.list_namespaced_pod(
                    namespace=ns,
                    label_selector=sel,
                    field_selector=f"spec.nodeName={node_name}",
                    _request_timeout=(10, 30),
                ).items or []
            except Exception as e:  # noqa: BLE001
                self.sink.write("pod_logs_list_error",
                                {"ns": ns, "selector": sel, "error": str(e)[:200]})
                summary["errors"] += 1
                continue
            for p in pods:
                pname = p.metadata.name
                key = (ns, pname)
                if key in seen_pods:
                    continue
                seen_pods.add(key)
                summary["pods"] += 1
                containers: list[str] = []
                containers += [c.name for c in (p.spec.init_containers or [])]
                containers += [c.name for c in (p.spec.containers or [])]
                for cname in containers:
                    try:
                        kw = dict(
                            name=pname, namespace=ns, container=cname,
                            timestamps=True, tail_lines=tail_lines,
                            _request_timeout=(10, 60),
                        )
                        if since_s is not None:
                            kw["since_seconds"] = since_s
                        logs: str = self.core.read_namespaced_pod_log(**kw)
                    except Exception as e:  # noqa: BLE001
                        self.sink.write("pod_logs_fetch_error",
                                        {"ns": ns, "pod": pname, "container": cname,
                                         "error": str(e)[:200]})
                        summary["errors"] += 1
                        continue
                    if not logs:
                        continue
                    fname = f"{ns}__{pname}__{cname}.log"
                    out_path = dest / fname
                    try:
                        out_path.write_text(logs)
                        summary["containers"] += 1
                        summary["bytes"] += len(logs)
                    except OSError as e:
                        self.sink.write("pod_logs_write_error",
                                        {"path": str(out_path), "error": str(e)[:200]})
                        summary["errors"] += 1
        self.sink.write("pod_logs_collected",
                        {"node": node_name, "dest": str(dest), **summary})
        return summary


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
