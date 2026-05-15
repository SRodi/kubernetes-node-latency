"""Shared helpers for shelling out to cloud CLIs."""
from __future__ import annotations

import logging
import shlex
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)


def run(cmd: list[str], *, check: bool = True, env: dict | None = None,
        capture: bool = False, cwd: str | Path | None = None,
        timeout: float | None = 1800.0) -> subprocess.CompletedProcess:
    """Shell out to a cloud CLI with a hard upper-bound timeout.

    ``timeout`` defaults to 30 minutes — long enough for cluster create on
    every supported provider, short enough that a stalled CLI eventually
    surfaces a ``TimeoutExpired`` instead of hanging the harness forever.
    Callers that need an unbounded wait can pass ``timeout=None``.
    """
    log.info("$ %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(
        cmd,
        check=check,
        env=env,
        text=True,
        capture_output=capture,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


def run_with_retry(cmd: list[str], *, retries: int = 6, initial_delay_s: float = 30.0,
                    max_delay_s: float = 180.0, retry_on: tuple[str, ...] = (),
                    env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command, retrying with exponential backoff on transient failures.

    Returns the final CompletedProcess (caller decides what to do with non-zero
    exit). Retries when the command's combined stdout+stderr contains any of
    the substrings in ``retry_on``.
    """
    delay = initial_delay_s
    last: subprocess.CompletedProcess | None = None
    for attempt in range(1, retries + 1):
        last = run(cmd, check=False, env=env, capture=True)
        if last.returncode == 0:
            if last.stdout:
                print(last.stdout, end="")
            return last
        combined = (last.stdout or "") + (last.stderr or "")
        if last.stderr:
            print(last.stderr, end="")
        if not retry_on or not any(s in combined for s in retry_on) or attempt == retries:
            return last
        log.warning("retryable failure (attempt %d/%d); sleeping %.0fs before retry",
                    attempt, retries, delay)
        time.sleep(delay)
        delay = min(delay * 2, max_delay_s)
    return last  # type: ignore[return-value]


def gke_wait_for_inflight_ops(cluster_name: str, region: str, *,
                                timeout_s: float = 900.0,
                                poll_interval_s: float = 15.0) -> None:
    """Block until no RUNNING ops exist on the cluster or its node-pools.

    Autopilot/Standard frequently has background ops (AUTO_REPAIR_NODES, UPGRADE_*,
    SET_NODE_POOL_*) right after a workload run. Issuing ``clusters delete`` while
    one is in flight returns HTTP 400 "Cluster is running incompatible operation".
    Polling beats wide exponential backoff: we usually wait <60s vs. up to ~10m
    of retry budget.
    """
    deadline = time.monotonic() + timeout_s
    filter_expr = f"targetLink~{cluster_name} AND status=RUNNING"
    while time.monotonic() < deadline:
        res = run(
            ["gcloud", "container", "operations", "list",
             "--region", region, "--filter", filter_expr,
             "--format", "value(name,type)"],
            check=False, capture=True,
        )
        lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return
        log.info("waiting for %d in-flight GKE op(s) on %s: %s",
                 len(lines), cluster_name, "; ".join(lines))
        time.sleep(poll_interval_s)
    log.warning("timed out waiting for in-flight GKE ops on %s; proceeding anyway",
                cluster_name)
