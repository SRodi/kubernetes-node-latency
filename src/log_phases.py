"""Fine-grained sub-phase extraction for three node-startup hotspots that
showed anomalously large durations against AKS's own managed-Cilium
baseline: `run:cni-installer` (azure-cns init container), `run:cilium-init-all`
(cilium init container), and the synthetic "Agent main container startup"
lane (the gap between the cilium-agent image being marked pulled and the
agent container actually running).

All three are opaque in the stock instrumentation because kubelet only
reports container `started_at`/`finished_at` at 1-second resolution via
the K8s API. This module parses the already-captured
(`--capture-logs minimal`) container logs — which carry sub-second
timestamps — to pin down exactly how much of each window is the
container's own internal work vs. an unexplained gap before/after it
(most likely node-level kubelet/containerd scheduling latency under
resource contention; see VHD-BUILD-LOG.md for the investigation this
was built to support).

Pure functions only (no I/O beyond reading already-fetched log text /
already-collected JSON structures), so this is unit-testable without a
cluster.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

_TS_LINE = re.compile(r"^(?P<ts>\S+)\s+(?P<rest>.*)$")


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_log_lines(text: str) -> list[tuple[datetime, str]]:
    """Split kubectl-logs `--timestamps` output into (ts, rest-of-line)."""
    out: list[tuple[datetime, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        m = _TS_LINE.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group("ts"))
        if ts is None:
            continue
        out.append((ts, m.group("rest")))
    return out


def parse_cilium_init_all(text: str) -> dict[str, Any]:
    """Extract the dominant sub-step (writing the cilium-cni binary to the
    hostPath mount) plus the overall log-visible span from the single
    `cilium-init-all` init container's log.
    """
    lines = _parse_log_lines(text)
    out: dict[str, Any] = {}
    if not lines:
        return out
    out["first_ts"] = lines[0][0]
    out["last_ts"] = lines[-1][0]
    install_start: datetime | None = None
    install_end: datetime | None = None
    for ts, msg in lines:
        if "Installing cilium-cni" in msg and install_start is None:
            install_start = ts
        if re.search(r"Wrote .*cilium-cni", msg) and install_end is None:
            install_end = ts
    if install_start is not None and install_end is not None:
        out["cni_binary_write_s"] = max(
            (install_end - install_start).total_seconds(), 0.0)
    return out


def parse_cni_installer(text: str) -> dict[str, Any]:
    """Extract the log-visible span from azure-cns's `cni-installer` init
    container (writes the azure-ipam binary). The work itself is tiny
    (two near-simultaneous log lines); the value of this is mainly to
    pin down `first_ts`/`last_ts` so callers can compute the gap to the
    next container's kubelet-observed start.
    """
    lines = _parse_log_lines(text)
    out: dict[str, Any] = {}
    if not lines:
        return out
    out["first_ts"] = lines[0][0]
    out["last_ts"] = lines[-1][0]
    return out


# Cilium-agent startup milestones, in the order they occur. Matched
# against the human-readable `msg="..."` field (sub-second timestamps
# come from the enclosing kubectl-logs `--timestamps` prefix, which is
# more reliable across cilium versions than the agent's own embedded
# `time=` field).
_AGENT_MILESTONES: dict[str, re.Pattern[str]] = {
    "initializing_daemon": re.compile(r'msg="Initializing daemon"'),
    "serving_cilium_api": re.compile(r'msg="Serving cilium API at'),
    "bpf_compiled_first": re.compile(r'msg="Compiled new BPF template"'),
    "program_attached_last": re.compile(r'msg="Program attached to device'),
    "serving_health_api": re.compile(r'msg="Serving cilium health API'),
}


def parse_cilium_agent(text: str) -> dict[str, Any]:
    """Extract cilium-agent's own internal startup milestones. On a
    healthy node these all land within ~1-2s of the container starting —
    used to prove/disprove whether a slow "Agent main container startup"
    lane is internal agent work vs. a pre-start scheduling gap.
    """
    lines = _parse_log_lines(text)
    out: dict[str, Any] = {}
    if not lines:
        return out
    out["first_ts"] = lines[0][0]
    out["last_ts"] = lines[-1][0]
    firsts: dict[str, datetime] = {}
    lasts: dict[str, datetime] = {}
    for ts, msg in lines:
        for key, pat in _AGENT_MILESTONES.items():
            if pat.search(msg):
                if key not in firsts:
                    firsts[key] = ts
                lasts[key] = ts
    for key in _AGENT_MILESTONES:
        if key in lasts:
            out[key] = lasts[key] if key.endswith("_last") else firsts[key]
    return out


def _find_log(log_dir: Path, container: str) -> Path | None:
    matches = sorted(log_dir.glob(f"*__{container}.log"))
    return matches[0] if matches else None


def _as_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        return _parse_ts(v)
    return None


def compute_phase_breakdown(rec: Any, log_dir: Path) -> dict[str, Any]:
    """Combine the per-container log parses above with the already-
    collected K8s-API container-lifecycle lists on `rec`
    (`node_container_starts`, `node_image_pulls`, `init_containers`) to
    produce a flat dict of scalar columns:

    - `cilium_init_all_log_span_s`, `cilium_init_all_cni_binary_write_s`,
      `cilium_init_all_post_log_gap_s` (kubelet-reported finish minus the
      container's own last log line — the unexplained tail).
    - `cni_installer_log_span_s`, `cni_installer_post_log_gap_s`
      (next container's kubelet-observed start minus this container's
      last log line).
    - `agent_pull_to_start_gap_s` (cilium-agent container start minus the
      agent image's `t_pulled`) and `agent_internal_bootstrap_s`
      (T3_cilium_ready minus agent container start) — isolates whether
      the "Agent main container startup" cost is a pre-start scheduling
      gap (large `agent_pull_to_start_gap_s`) or genuine agent-internal
      work (large `agent_internal_bootstrap_s`).
    - `agent_log_<milestone>_offset_s` for each milestone in
      `_AGENT_MILESTONES`, relative to the agent container's kubelet-
      observed start.

    Best-effort: any missing input (no log capture, no init-container
    data, etc.) simply omits the corresponding columns. Never raises.
    """
    out: dict[str, Any] = {}
    log_dir = Path(log_dir)

    # ---- cilium-init-all ----
    p = _find_log(log_dir, "cilium-init-all")
    init_all_parsed: dict[str, Any] = {}
    if p is not None:
        try:
            init_all_parsed = parse_cilium_init_all(p.read_text())
        except OSError:
            init_all_parsed = {}
    if init_all_parsed:
        if "first_ts" in init_all_parsed and "last_ts" in init_all_parsed:
            out["cilium_init_all_log_span_s"] = max(
                (init_all_parsed["last_ts"] - init_all_parsed["first_ts"]).total_seconds(), 0.0)
        if "cni_binary_write_s" in init_all_parsed:
            out["cilium_init_all_cni_binary_write_s"] = init_all_parsed["cni_binary_write_s"]
        last_ts = init_all_parsed.get("last_ts")
        if last_ts is not None and rec.init_containers:
            for ic in rec.init_containers:
                if ic.get("name") == "cilium-init-all":
                    fa = _as_dt(ic.get("finished_at"))
                    if fa is not None:
                        out["cilium_init_all_post_log_gap_s"] = max(
                            (fa - last_ts).total_seconds(), 0.0)
                    break

    # ---- cni-installer (azure-cns) ----
    p = _find_log(log_dir, "cni-installer")
    cni_installer_parsed: dict[str, Any] = {}
    if p is not None:
        try:
            cni_installer_parsed = parse_cni_installer(p.read_text())
        except OSError:
            cni_installer_parsed = {}
    if cni_installer_parsed:
        if "first_ts" in cni_installer_parsed and "last_ts" in cni_installer_parsed:
            out["cni_installer_log_span_s"] = max(
                (cni_installer_parsed["last_ts"] - cni_installer_parsed["first_ts"]).total_seconds(), 0.0)
        last_ts = cni_installer_parsed.get("last_ts")
        if last_ts is not None and rec.node_container_starts:
            # Next container to start in the same pod (azure-cns's main
            # `cns-container`) — the gap to it is the unexplained tail.
            main_starts = [
                _as_dt(s.get("t_started"))
                for s in rec.node_container_starts
                if s.get("pod", "").startswith("azure-cns") and not s.get("init")
            ]
            main_starts = [t for t in main_starts if t is not None]
            if main_starts:
                out["cni_installer_post_log_gap_s"] = max(
                    (min(main_starts) - last_ts).total_seconds(), 0.0)

    # ---- cilium-agent (main container "Agent main container startup") ----
    p = _find_log(log_dir, "cilium-agent")
    agent_parsed: dict[str, Any] = {}
    if p is not None:
        try:
            agent_parsed = parse_cilium_agent(p.read_text())
        except OSError:
            agent_parsed = {}
    agent_start: datetime | None = None
    if rec.node_container_starts:
        starts = [
            _as_dt(s.get("t_started"))
            for s in rec.node_container_starts
            if s.get("container") == "cilium-agent" and not s.get("init")
        ]
        starts = [t for t in starts if t is not None]
        if starts:
            agent_start = min(starts)
    if agent_start is not None:
        if rec.node_image_pulls:
            agent_pulls = [
                _as_dt(p_.get("t_pulled"))
                for p_ in rec.node_image_pulls
                if p_.get("container") == "cilium-agent" and p_.get("t_pulled")
            ]
            agent_pulls = [t for t in agent_pulls if t is not None]
            if agent_pulls:
                out["agent_pull_to_start_gap_s"] = max(
                    (agent_start - max(agent_pulls)).total_seconds(), 0.0)
        if rec.T3_cilium_ready is not None:
            out["agent_internal_bootstrap_s"] = max(
                (rec.T3_cilium_ready - agent_start).total_seconds(), 0.0)
        for key in _AGENT_MILESTONES:
            if key in agent_parsed:
                out[f"agent_log_{key}_offset_s"] = max(
                    (agent_parsed[key] - agent_start).total_seconds(), 0.0)

    return out
