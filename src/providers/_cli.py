"""Shared helpers for shelling out to cloud CLIs."""
from __future__ import annotations

import logging
import shlex
import subprocess
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
