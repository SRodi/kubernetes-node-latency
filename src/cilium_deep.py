"""Tier-1 "deep Cilium" capture.

Once T3 fires for an iteration, exec into the cilium-agent pod on the new
node and capture two artefacts:

  * `cilium status -o json --verbose` — has a `bootstrap` block with
    per-phase durations (k8sInit, restoreState, daemonInit, bpfBase, ipam,
    proxyInit, endpointRestore, total, …) plus controller, ipam, kvstore,
    encryption, kube-proxy-replacement health.
  * `curl -s localhost:<port>/metrics` — Prometheus exposition; we keep
    the whole thing for offline analysis and parse a small set of
    histograms (bootstrap, endpoint regeneration, BPF map pressure)
    into headline numbers for `iterations.csv`.

Both artefacts are written under `<run_dir>/iter-<NNN>/`. Per-iteration
headline numbers are merged into the IterationRecord via
`apply_to_record()`.

This is opt-in (cfg.cni.deep) because the exec adds ~2-5s per iteration.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from kubernetes import client
from kubernetes.stream import stream

log = logging.getLogger(__name__)


# ---- Prometheus parsing ---------------------------------------------------

# Match a single sample line: "name{labels} value [timestamp]"
_PROM_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)(?:\s+\d+)?\s*$"
)
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _parse_labels(s: str | None) -> dict[str, str]:
    if not s:
        return {}
    return {m.group(1): m.group(2) for m in _LABEL.finditer(s)}


def _iter_samples(metrics_text: str):
    for line in metrics_text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _PROM_LINE.match(line)
        if not m:
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        yield m.group("name"), _parse_labels(m.group("labels")), value


def parse_metrics(metrics_text: str) -> dict[str, Any]:
    """Pull a small set of headline numbers out of a Prometheus dump.

    Headlines are stable across Cilium versions used by GKE/AKS:
      * cilium_bootstrap_seconds (or cilium_agent_bootstrap_seconds) — per-scope
      * cilium_endpoint_regeneration_time_stats_seconds — buckets per scope
      * cilium_endpoint_state — gauge per state
      * cilium_identity_count — gauge
      * cilium_bpf_map_pressure — gauge per map
    """
    bootstrap: dict[str, float] = {}
    regen_sums: dict[str, float] = {}
    regen_counts: dict[str, float] = {}
    endpoint_state: dict[str, float] = {}
    identity_count: float | None = None
    map_pressure_max: float = 0.0

    for name, labels, value in _iter_samples(metrics_text):
        if name in ("cilium_bootstrap_seconds", "cilium_agent_bootstrap_seconds"):
            scope = labels.get("scope") or labels.get("subsystem") or "total"
            bootstrap[scope] = value
        elif name == "cilium_endpoint_regeneration_time_stats_seconds_sum":
            regen_sums[labels.get("scope", "total")] = value
        elif name == "cilium_endpoint_regeneration_time_stats_seconds_count":
            regen_counts[labels.get("scope", "total")] = value
        elif name == "cilium_endpoint_state":
            endpoint_state[labels.get("state", "?")] = value
        elif name == "cilium_identity_count":
            identity_count = value
        elif name == "cilium_bpf_map_pressure":
            map_pressure_max = max(map_pressure_max, value)

    regen_avg = {
        scope: (regen_sums[scope] / regen_counts[scope])
        for scope in regen_sums
        if regen_counts.get(scope, 0) > 0
    }
    return {
        "bootstrap": bootstrap,
        "endpoint_regeneration_avg_s": regen_avg,
        "endpoint_state": endpoint_state,
        "identity_count": identity_count,
        "bpf_map_pressure_max": map_pressure_max,
    }


# ---- exec helpers ---------------------------------------------------------

def _exec(core: client.CoreV1Api, *, namespace: str, pod: str, container: str,
           command: list[str], timeout_s: int = 30) -> tuple[str, int]:
    """Synchronously exec a command in a container; return (stdout, rc).

    On any failure we return (stderr/exception text, non-zero rc) rather
    than raising — deep-capture must never break a successful iteration.
    """
    try:
        resp = stream(
            core.connect_get_namespaced_pod_exec,
            pod, namespace, container=container, command=command,
            stderr=True, stdin=False, stdout=True, tty=False,
            _preload_content=False,
        )
        out_chunks: list[str] = []
        err_chunks: list[str] = []
        resp.run_forever(timeout=timeout_s)
        if resp.peek_stdout():
            out_chunks.append(resp.read_stdout())
        if resp.peek_stderr():
            err_chunks.append(resp.read_stderr())
        rc = 0
        try:
            status = resp.read_channel(3, timeout=2)  # error channel
            if status:
                payload = json.loads(status)
                if payload.get("status") == "Failure":
                    rc = int(((payload.get("details") or {}).get("causes") or [{}])
                              [0].get("message", "1") or 1)
        except Exception:
            pass
        out = "".join(out_chunks)
        if rc != 0 and not out:
            out = "".join(err_chunks)
        return out, rc
    except Exception as e:  # noqa: BLE001
        log.warning("exec failed in %s/%s: %s", namespace, pod, e)
        return f"exec_error: {e}", 1


def fetch_status(core: client.CoreV1Api, *, namespace: str, pod: str,
                  container: str) -> dict | None:
    out, rc = _exec(core, namespace=namespace, pod=pod, container=container,
                     command=["cilium", "status", "-o", "json", "--verbose"])
    if rc != 0 or not out.strip():
        log.info("cilium status unavailable on %s/%s (rc=%s)", namespace, pod, rc)
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        log.warning("cilium status returned non-JSON: %s", e)
        return None


def fetch_metrics(core: client.CoreV1Api, *, namespace: str, pod: str,
                   container: str, ports: list[int]) -> str | None:
    for port in ports:
        out, rc = _exec(
            core, namespace=namespace, pod=pod, container=container,
            command=["sh", "-c",
                     f"command -v curl >/dev/null && curl -sf http://127.0.0.1:{port}/metrics "
                     f"|| wget -qO- http://127.0.0.1:{port}/metrics"],
        )
        if rc == 0 and out and "cilium_" in out:
            log.debug("got Cilium metrics on port %d", port)
            return out
    log.info("cilium metrics not reachable on ports %s", ports)
    return None


# ---- top-level entry ------------------------------------------------------

def collect(core: client.CoreV1Api, *, agent_pod, probe, iter_dir: Path,
             metrics_ports: list[int]) -> dict[str, Any]:
    """Capture status + metrics for one agent pod; persist to iter_dir.

    Returns a dict of headline numbers suitable for merging into
    IterationRecord.deep_cilium.
    """
    iter_dir.mkdir(parents=True, exist_ok=True)
    headline: dict[str, Any] = {}

    status = fetch_status(core, namespace=agent_pod.metadata.namespace,
                          pod=agent_pod.metadata.name,
                          container=probe.container_name)
    if status is not None:
        (iter_dir / "cilium_status.json").write_text(
            json.dumps(status, indent=2, default=str))
        bootstrap = (status.get("bootstrap") or {})
        # Cilium reports bootstrap durations either as nanoseconds or as
        # seconds-with-unit-suffix; normalise to float seconds.
        b: dict[str, float] = {}
        for k, v in bootstrap.items():
            try:
                if isinstance(v, (int, float)):
                    # > 1e6 → almost certainly nanoseconds
                    b[k] = float(v) / 1e9 if abs(v) > 1e6 else float(v)
                elif isinstance(v, str) and v.endswith("ms"):
                    b[k] = float(v[:-2]) / 1000.0
                elif isinstance(v, str) and v.endswith("s"):
                    b[k] = float(v[:-1])
            except (TypeError, ValueError):
                continue
        headline["bootstrap"] = b
        ipam = (status.get("ipam") or {}).get("status")
        headline["ipam_mode"] = (status.get("ipam") or {}).get("mode")
        headline["ipam_status"] = ipam
        kpr = (status.get("kube-proxy-replacement") or {}).get("mode")
        headline["kube_proxy_replacement"] = kpr
        headline["cilium_version"] = (status.get("cilium") or {}).get("version")

    metrics_text = fetch_metrics(core, namespace=agent_pod.metadata.namespace,
                                  pod=agent_pod.metadata.name,
                                  container=probe.container_name,
                                  ports=metrics_ports)
    if metrics_text is not None:
        (iter_dir / "cilium_metrics.txt").write_text(metrics_text)
        headline["metrics"] = parse_metrics(metrics_text)

    if headline:
        (iter_dir / "cilium_deep_headline.json").write_text(
            json.dumps(headline, indent=2, default=str))
    return headline


# ---- record integration ---------------------------------------------------

# Stable column names exposed in iterations.csv when --deep-cilium is set.
HEADLINE_COLUMNS = (
    "cilium_bootstrap_total_s",
    "cilium_bootstrap_k8s_init_s",
    "cilium_bootstrap_restore_s",
    "cilium_bootstrap_bpf_base_s",
    "cilium_bootstrap_ipam_s",
    "cilium_bootstrap_proxy_s",
    "cilium_endpoint_regen_avg_s",
    "cilium_identity_count",
    "cilium_version",
)


def headline_to_columns(headline: dict[str, Any] | None) -> dict[str, Any]:
    """Project the deep-cilium headline dict to the stable CSV columns."""
    if not headline:
        return {c: None for c in HEADLINE_COLUMNS}
    b = headline.get("bootstrap") or {}
    metrics = headline.get("metrics") or {}
    regen = (metrics.get("endpoint_regeneration_avg_s") or {})
    regen_avg = regen.get("total") or (sum(regen.values()) / len(regen) if regen else None)
    return {
        "cilium_bootstrap_total_s": b.get("total"),
        "cilium_bootstrap_k8s_init_s": b.get("k8sInit") or b.get("k8s_init"),
        "cilium_bootstrap_restore_s": b.get("restoreState") or b.get("restore_state"),
        "cilium_bootstrap_bpf_base_s": b.get("bpfBase") or b.get("bpf_base"),
        "cilium_bootstrap_ipam_s": b.get("ipam"),
        "cilium_bootstrap_proxy_s": b.get("proxyInit") or b.get("proxy_init"),
        "cilium_endpoint_regen_avg_s": regen_avg,
        "cilium_identity_count": metrics.get("identity_count"),
        "cilium_version": headline.get("cilium_version"),
    }
