"""Shared helpers for shelling out to cloud CLIs."""
from __future__ import annotations

import logging
import shlex
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)


def run(cmd: list[str], *, check: bool = True, env: dict | None = None,
        capture: bool = False, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(
        cmd,
        check=check,
        env=env,
        text=True,
        capture_output=capture,
        cwd=str(cwd) if cwd else None,
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
