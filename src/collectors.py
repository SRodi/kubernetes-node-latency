"""K8s watchers + log scanners that capture T0-T4 for one iteration."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
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
    def wait_for_new_node(self, before_nodes: set[str], timeout_s: int) -> tuple[str, datetime]:
        deadline = time.monotonic() + timeout_s
        w = watch.Watch()
        try:
            for ev in w.stream(self.core.list_node, timeout_seconds=timeout_s):
                node = ev["object"]
                name = node.metadata.name
                if name in before_nodes:
                    continue
                ts = _parse_k8s_time(node.metadata.creation_timestamp) or utcnow()
                self.sink.write("node_added", {"name": name, "creationTimestamp": ts.isoformat()})
                return name, ts
                if time.monotonic() > deadline:
                    break
        finally:
            w.stop()
        raise TimeoutError("no new node observed before timeout")

    # ----- T4: Ready=True transition -----
    def wait_for_node_ready(self, node_name: str, timeout_s: int) -> datetime:
        deadline = time.monotonic() + timeout_s
        w = watch.Watch()
        try:
            field = f"metadata.name={node_name}"
            for ev in w.stream(self.core.list_node, field_selector=field,
                               timeout_seconds=timeout_s):
                node = ev["object"]
                for c in (node.status.conditions or []):
                    if c.type == "Ready" and c.status == "True":
                        ts = _parse_k8s_time(c.last_transition_time) or utcnow()
                        self.sink.write("node_ready", {"name": node_name, "ts": ts.isoformat()})
                        return ts
                if time.monotonic() > deadline:
                    break
        finally:
            w.stop()
        raise TimeoutError(f"node {node_name} never became Ready=True")

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
        w = watch.Watch()
        try:
            for ev in w.stream(
                self.core.list_namespaced_pod, namespace=self.probe.namespace,
                field_selector=f"metadata.name={pod_name}", timeout_seconds=timeout_s,
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
