"""Tests for T5 (trigger pod Running) capture and pod-log fetch retries."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes import client

from src.collectors import Collector, _first_container_running_start


class _Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


def _running_pod(started_at: datetime | None) -> SimpleNamespace:
    running = SimpleNamespace(started_at=started_at) if started_at else None
    state = SimpleNamespace(running=running)
    cs = SimpleNamespace(state=state)
    return SimpleNamespace(status=SimpleNamespace(container_statuses=[cs]))


def _pending_pod() -> SimpleNamespace:
    return SimpleNamespace(status=SimpleNamespace(container_statuses=None))


def _probe() -> MagicMock:
    p = MagicMock()
    p.namespace = "default"
    p.container_name = "app"
    return p


def test_first_container_running_start_returns_ts():
    ts = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    assert _first_container_running_start(_running_pod(ts)) == ts


def test_first_container_running_start_pending_is_none():
    assert _first_container_running_start(_pending_pod()) is None


def test_wait_for_pod_running_sync_read_fallback(monkeypatch):
    """Watch yields nothing; the final sync read recovers T5."""
    import src.collectors as collectors

    empty_watch = MagicMock()
    empty_watch.stream.return_value = []
    monkeypatch.setattr(collectors.watch, "Watch", lambda: empty_watch)

    ts = datetime(2026, 7, 30, 12, 0, 5, tzinfo=timezone.utc)
    core = MagicMock()
    core.read_namespaced_pod.return_value = _running_pod(ts)

    col = Collector(core, _probe(), _Sink())
    assert col.wait_for_pod_running("trig-1", "default", timeout_s=30) == ts


def test_wait_for_pod_running_never_running_returns_none(monkeypatch):
    import src.collectors as collectors

    empty_watch = MagicMock()
    empty_watch.stream.return_value = []
    monkeypatch.setattr(collectors.watch, "Watch", lambda: empty_watch)

    core = MagicMock()
    core.read_namespaced_pod.return_value = _pending_pod()

    col = Collector(core, _probe(), _Sink())
    assert col.wait_for_pod_running("trig-1", "default", timeout_s=30) is None


def test_collect_pod_logs_retries_transient_500(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("src.collectors.time.sleep", lambda *_: None)

    calls = {"n": 0}

    def flaky_read(**_kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise client.ApiException(status=500, reason="proxy error")
        return "2026-07-30T12:00:00Z hello\n"

    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[SimpleNamespace(
            metadata=SimpleNamespace(name="p1"),
            spec=SimpleNamespace(
                containers=[SimpleNamespace(name="app")],
                init_containers=None),
        )]
    )
    core.read_namespaced_pod_log.side_effect = flaky_read

    col = Collector(core, _probe(), _Sink())
    summary = col.collect_pod_logs(
        "node-1", tmp_path, targets=[("default", "k8s-app=app")],
        fetch_backoff_s=0.0,
    )
    assert calls["n"] == 3
    assert summary["errors"] == 0
    assert summary["containers"] == 1


def test_collect_pod_logs_terminal_404_no_retry(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("src.collectors.time.sleep", lambda *_: None)

    calls = {"n": 0}

    def read_404(**_kw):
        calls["n"] += 1
        raise client.ApiException(status=404, reason="not found")

    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[SimpleNamespace(
            metadata=SimpleNamespace(name="p1"),
            spec=SimpleNamespace(
                containers=[SimpleNamespace(name="app")],
                init_containers=None),
        )]
    )
    core.read_namespaced_pod_log.side_effect = read_404

    col = Collector(core, _probe(), _Sink())
    summary = col.collect_pod_logs(
        "node-1", tmp_path, targets=[("default", "k8s-app=app")],
        fetch_backoff_s=0.0,
    )
    assert calls["n"] == 1
    assert summary["errors"] == 1
