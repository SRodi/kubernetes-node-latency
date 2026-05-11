"""Synthetic fixtures + unit tests for parsers, records, analysis, plotting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.analysis import aggregate, to_dataframe, write_outputs
from src.collectors import _extract_log_ts, _parse_k8s_time
from src.config import Config
from src.plotting import plot_all
from src.records import IterationRecord


def _ts(s: float) -> datetime:
    return datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=s)


def synthetic_records(n: int = 5) -> list[IterationRecord]:
    out = []
    for i in range(1, n + 1):
        r = IterationRecord(iteration=i, run_id="t", provider="gke_autopilot", region="x")
        r.pod_name = f"p{i}"; r.node_name = f"node-{i}"
        r.T0_pod_created = _ts(0 + i)
        r.T1_node_registered = _ts(5 + i)
        r.T2_cilium_started = _ts(8 + i)
        r.T3_cilium_ready = _ts(20 + i)
        r.T4_node_ready = _ts(22 + i)
        r.status = "success"
        out.append(r)
    return out


def test_parse_k8s_time_roundtrip():
    assert _parse_k8s_time("2025-01-01T12:00:00Z").year == 2025
    assert _parse_k8s_time(None) is None


def test_extract_log_ts():
    line = '2025-01-01T12:34:56.123Z stdout F level=info msg="All Cilium daemons are ready"'
    ts = _extract_log_ts(line)
    assert ts is not None and ts.year == 2025

    line2 = 'time="2025-06-01T00:00:00Z" level=info msg=foo'
    assert _extract_log_ts(line2) is not None


def test_record_row_math():
    recs = synthetic_records(1)
    row = recs[0].to_row()
    assert row["node_startup_latency_s"] == 22.0  # T4 - T0
    assert row["node_register_latency_s"] == 5.0
    assert row["cilium_init_duration_s"] == 12.0
    assert row["cni_induced_delay_s"] == 2.0


def test_aggregate_stats():
    df = to_dataframe(synthetic_records(5))
    agg = aggregate(df)
    row = agg[agg["metric"] == "node_startup_latency_s"].iloc[0]
    assert row["count"] == 5
    assert row["min"] == 22.0
    assert row["max"] == 22.0
    assert row["mean"] == 22.0


def test_write_outputs_and_plots(tmp_path: Path):
    recs = synthetic_records(8)
    out = tmp_path / "runX"
    summary = write_outputs(recs, out, run_id="runX", provider="gke_autopilot", region="x")
    assert (out / "iterations.csv").exists()
    assert (out / "summary.md").exists()
    assert summary["iterations"] == 8
    paths = plot_all(out / "iterations.csv", out / "plots")
    assert all(p.exists() for p in paths)
    assert len(paths) >= 4


def test_config_from_dict_roundtrip():
    cfg = Config.from_dict({"provider": "existing", "iterations": 3})
    assert cfg.provider == "existing"
    assert cfg.iterations == 3
    assert cfg.trigger_pod.cpu == "1500m"


# --- helpers for the new T2/T3 paths ---

class _Obj:
    def __init__(self, **kw): self.__dict__.update(kw)


def _make_pod(*, container_running=None, container_terminated=None,
              init_finished=None, start_time=None, ready_condition=None):
    cs = _Obj(name="cilium-agent",
              state=_Obj(running=container_running, terminated=container_terminated, waiting=None),
              last_state=_Obj(running=None, terminated=None, waiting=None))
    init_cs = []
    if init_finished is not None:
        init_cs = [_Obj(state=_Obj(terminated=_Obj(finished_at=init_finished, started_at=None),
                                     running=None, waiting=None))]
    conds = []
    if ready_condition is not None:
        conds.append(_Obj(type="Ready", status="True", last_transition_time=ready_condition))
    pod = _Obj(metadata=_Obj(name="anetd-x", namespace="kube-system"),
               status=_Obj(container_statuses=[cs], init_container_statuses=init_cs,
                           start_time=start_time, conditions=conds))
    return pod


def test_container_started_at_uses_running_state():
    from src.collectors import _container_started_at
    pod = _make_pod(container_running=_Obj(started_at="2025-01-01T00:00:05Z"))
    ts = _container_started_at(pod, "cilium-agent")
    assert ts is not None and ts.second == 5


def test_container_started_at_falls_back_to_init_finish():
    from src.collectors import _container_started_at
    pod = _make_pod(init_finished="2025-01-01T00:00:09Z", start_time="2025-01-01T00:00:01Z")
    ts = _container_started_at(pod, "cilium-agent")
    assert ts is not None and ts.second == 9


def test_container_started_at_falls_back_to_pod_starttime():
    from src.collectors import _container_started_at
    pod = _make_pod(start_time="2025-01-01T00:00:01Z")
    ts = _container_started_at(pod, "cilium-agent")
    assert ts is not None and ts.second == 1


def test_pod_ready_transition_picks_ready_true():
    from src.collectors import _pod_ready_transition
    pod = _make_pod(ready_condition="2025-01-01T00:00:30Z")
    ts = _pod_ready_transition(pod, "Ready")
    assert ts is not None and ts.second == 30


def test_pod_ready_transition_returns_none_when_absent():
    from src.collectors import _pod_ready_transition
    pod = _make_pod()
    assert _pod_ready_transition(pod, "Ready") is None


def test_wait_for_new_node_skips_nodes_predating_trigger(monkeypatch):
    """A node whose creationTimestamp is well before T0 must be ignored, the
    next genuinely-post-T0 node accepted."""
    from datetime import datetime, timezone
    from src.collectors import Collector, EventSink
    from src.cni import get as get_probe

    class FakeMeta:
        def __init__(self, name, ts): self.name = name; self.creation_timestamp = ts

    class FakeNode:
        def __init__(self, name, ts): self.metadata = FakeMeta(name, ts)

    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    stale = FakeNode("stale", datetime(2025, 1, 1, 11, 59, 0, tzinfo=timezone.utc))
    fresh = FakeNode("fresh", datetime(2025, 1, 1, 12, 0, 5, tzinfo=timezone.utc))

    class FakeWatch:
        def stream(self, *a, **kw):
            yield {"object": stale}
            yield {"object": fresh}
        def stop(self): pass

    monkeypatch.setattr("src.collectors.watch.Watch", FakeWatch)

    sink_calls = []
    class S:
        def write(self, kind, obj): sink_calls.append((kind, obj))

    class FakeCore:
        def list_node(self, **kw): return None  # never actually called
    c = Collector(core=FakeCore(), probe=get_probe("cilium_dpv2"), sink=S())  # type: ignore[arg-type]
    name, ts = c.wait_for_new_node(set(), timeout_s=5, not_before=t0)
    assert name == "fresh"
    assert ts.minute == 0 and ts.second == 5
    kinds = [k for k, _ in sink_calls]
    assert "node_pre_dates_trigger" in kinds
    assert "node_added" in kinds
