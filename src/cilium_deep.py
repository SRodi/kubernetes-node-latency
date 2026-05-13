"""Tier-1 "deep Cilium" capture — universal scraper-Pod strategy.

Once T3 fires, capture the Cilium agent's Prometheus metrics endpoint
(`/metrics` on port 9962/9090/etc.) for the new node and persist them
under `<run_dir>/iter-<NNN>/cilium_metrics.txt`. Headline numbers
(bootstrap timings, endpoint-regen averages, identity count, agent
version) are parsed from the metrics text and merged into
`iterations.csv` via `headline_to_columns()`.

Why a scraper Pod (and not exec / pod-proxy / ephemeral container)?
  GKE Autopilot's Warden blocks every operation that targets a
  managed kube-system Pod (exec, proxy, ephemeral containers). And
  Autopilot also blocks `hostNetwork: true` and privileged Pods. The
  one technique that works on **all** platforms is a vanilla user-
  namespace Pod that dials the agent's PodIP. Since the Cilium agent
  always runs hostNetwork, its PodIP equals the node's primary IP, and
  the metrics port is reachable from any Pod scheduled on the same
  node — no special permissions required.

This also covers AKS Azure-CNI (distroless cilium image, no shell),
AKS BYOCNI, GKE Standard DPv2, and any user-managed Cilium DaemonSet.

Adds ~3-5s per iteration (scraper image pull is cached after iter 1).
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from kubernetes import client

log = logging.getLogger(__name__)

DEFAULT_SCRAPER_IMAGE = "curlimages/curl:8.11.1"
DEFAULT_SCRAPER_NAMESPACE = "default"


# ---- Prometheus parsing ---------------------------------------------------

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
    """Pull a small set of headline numbers out of a Prometheus dump."""
    bootstrap: dict[str, float] = {}
    regen_sums: dict[str, float] = {}
    regen_counts: dict[str, float] = {}
    endpoint_state: dict[str, float] = {}
    identity_count: float | None = None
    map_pressure_max: float = 0.0
    version: str | None = None

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
        elif name == "cilium_version" or name == "cilium_version_info":
            v = labels.get("version") or labels.get("ver")
            if v:
                version = v

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
        "version": version,
    }


# ---- scraper Pod ----------------------------------------------------------

def _scraper_manifest(*, name: str, namespace: str, node_name: str,
                       agent_ip: str, ports: list[int], image: str) -> dict:
    """Build a minimal Pod that curls the agent's metrics endpoint.

    Pinned to the same node via `nodeName` so:
      * we never trigger autoscaler / NAP for this Pod, and
      * traffic stays on-host for fastest path.

    The shell tries each port in turn, prints a one-line probe summary
    per port (HTTP code + bytes), then on the first port that returns
    Cilium metrics, prints them and exits. Always exits 0 so diagnostic
    output is preserved in pod logs even when no port works.

    Resource requests are sized to satisfy Autopilot's per-Pod minimums
    (250m CPU / 0.5 GiB) without pushing the new node out of headroom
    (the trigger Pod requesting 1500m is still on it at this point).
    """
    port_list = " ".join(str(p) for p in ports)
    script = (
        f'set +e; AGENT_IP="{agent_ip}"; PORTS="{port_list}"; '
        'for p in $PORTS; do '
        '  body=$(curl -s -m 5 -o - -w "\\nHTTP=%{http_code} BYTES=%{size_download}\\n" '
        '         "http://$AGENT_IP:$p/metrics" 2>&1); '
        '  echo "=== probe port=$p ==="; '
        '  echo "$body" | tail -3; '
        '  if echo "$body" | grep -q "^cilium_"; then '
        '    echo "=== METRICS port=$p ==="; '
        '    echo "$body" | sed "/^HTTP=/d"; exit 0; '
        '  fi; '
        'done; exit 0'
    )
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "node-startup-latency", "role": "cilium-scraper"},
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeName": node_name,
            "terminationGracePeriodSeconds": 1,
            "containers": [{
                "name": "scraper",
                "image": image,
                "command": ["sh", "-c", script],
                "resources": {
                    "requests": {"cpu": "250m", "memory": "512Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
            }],
        },
    }


def _wait_pod_terminal(core: client.CoreV1Api, *, namespace: str, name: str,
                        timeout_s: int) -> str | None:
    """Poll until Pod phase is Succeeded/Failed; return the phase or None."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            p = core.read_namespaced_pod(name, namespace)
        except client.ApiException as e:
            log.debug("read scraper pod failed: %s", e.reason)
            return None
        phase = (p.status and p.status.phase) or "Pending"
        if phase in ("Succeeded", "Failed"):
            return phase
        time.sleep(0.5)
    return None


def _delete_pod_quiet(core: client.CoreV1Api, namespace: str, name: str) -> None:
    try:
        core.delete_namespaced_pod(
            name, namespace,
            body=client.V1DeleteOptions(grace_period_seconds=0,
                                         propagation_policy="Background"))
    except client.ApiException as e:
        if e.status not in (404, 410):
            log.debug("scraper cleanup failed: %s", e.reason)


def _resolve_agent_ip(core: client.CoreV1Api, *, namespace: str, name: str,
                       retries: int = 8, backoff_s: float = 2.0) -> str | None:
    """Re-fetch the agent Pod until PodIP is populated (or retries exhausted)."""
    for _ in range(retries):
        try:
            p = core.read_namespaced_pod(name, namespace)
        except client.ApiException as e:
            log.debug("agent pod re-read failed: %s", e.reason)
            return None
        ip = (p.status and p.status.pod_ip) or None
        if ip:
            return ip
        time.sleep(backoff_s)
    return None


def _discover_agent_ports(agent_pod, container_name: str | None) -> list[int]:
    """Return TCP container ports declared by the agent container.

    Looks at the named container first (e.g. `cilium-agent`), falls back to
    *every* container in the Pod. We skip ports < 1024 (BGP, DNS, etc.) and
    typical health-probe ports (8080) where /metrics is unlikely to live.
    """
    out: list[int] = []
    seen: set[int] = set()
    containers = (agent_pod.spec.containers or []) if agent_pod.spec else []
    for c in containers:
        if container_name and c.name != container_name and out:
            continue
        for p in (c.ports or []):
            proto = (getattr(p, "protocol", None) or "TCP").upper()
            port = getattr(p, "container_port", None)
            if proto != "TCP" or not port or port < 1024:
                continue
            if port in seen:
                continue
            seen.add(port)
            out.append(port)
    return out


def fetch_metrics(core: client.CoreV1Api, *, agent_pod, node_name: str,
                   ports: list[int], image: str = DEFAULT_SCRAPER_IMAGE,
                   namespace: str = DEFAULT_SCRAPER_NAMESPACE,
                   timeout_s: int = 60,
                   container_name: str | None = None) -> str | None:
    """Run a one-shot scraper Pod against the agent's PodIP, return its log.

    `ports` is the user-configured fallback list. Ports declared in the
    agent's Pod spec are probed first (covers GKE anetd which uses non-
    upstream ports and wouldn't be in the default list).

    Returns the metrics text on success, None on any failure (the iteration
    must never break because of deep capture). The returned text may include
    a short diagnostic preamble from the scraper (per-port HTTP probe lines)
    above a `=== METRICS port=<P> ===` separator.
    """
    agent_ip = (agent_pod.status and agent_pod.status.pod_ip) or None
    if not agent_ip:
        agent_ip = _resolve_agent_ip(
            core, namespace=agent_pod.metadata.namespace,
            name=agent_pod.metadata.name)
    if not agent_ip:
        log.info("agent pod has no PodIP yet; skipping deep capture")
        return None

    declared = _discover_agent_ports(agent_pod, container_name)
    # Declared ports first (most likely to work), then user-configured
    # fallbacks the agent didn't advertise.
    probe_ports = declared + [p for p in ports if p not in declared]
    if declared:
        log.debug("scraper will probe ports %s (declared %s, fallback %s)",
                   probe_ports, declared, ports)

    name = f"cilium-scrape-{uuid.uuid4().hex[:8]}"
    manifest = _scraper_manifest(name=name, namespace=namespace,
                                  node_name=node_name, agent_ip=agent_ip,
                                  ports=probe_ports, image=image)
    try:
        core.create_namespaced_pod(namespace, manifest)
    except client.ApiException as e:
        log.warning("scraper pod create failed (%s): %s", e.status, e.reason)
        return None

    try:
        phase = _wait_pod_terminal(core, namespace=namespace, name=name,
                                    timeout_s=timeout_s)
        if phase is None:
            log.info("scraper pod %s did not finish within %ss", name, timeout_s)
            return None
        logs = None
        last_err: Exception | None = None
        for _ in range(4):
            try:
                logs = core.read_namespaced_pod_log(
                    name=name, namespace=namespace, container="scraper")
                break
            except client.ApiException as e:
                last_err = e
                if e.status not in (400, 404):
                    break
                time.sleep(0.5)
        if logs is None:
            log.warning("scraper log read failed: %s",
                         getattr(last_err, "reason", last_err))
            return None
        if not logs or "cilium_" not in logs:
            tail = "\n".join((logs or "").splitlines()[-12:])
            log.info("scraper pod returned no Cilium metrics on ports %s "
                     "(phase=%s, log_len=%d). Tail: %s",
                     ports, phase, len(logs or ""), tail or "(empty)")
            return logs or None  # surface diag log to caller for persistence
        return logs
    finally:
        _delete_pod_quiet(core, namespace, name)


# ---- top-level entry ------------------------------------------------------

def collect(core: client.CoreV1Api, *, agent_pod, probe, node_name: str,
             iter_dir: Path, metrics_ports: list[int],
             scraper_image: str = DEFAULT_SCRAPER_IMAGE,
             scraper_namespace: str = DEFAULT_SCRAPER_NAMESPACE,
             scraper_timeout_s: int = 60) -> dict[str, Any]:
    """Collect Cilium metrics for the agent on `node_name`.

    Persists `cilium_metrics.txt` and `cilium_deep_headline.json` under
    `iter_dir` only when something was actually captured (no empty dirs).
    Returns the headline dict for merging into IterationRecord.
    """
    headline: dict[str, Any] = {}

    raw = fetch_metrics(
        core, agent_pod=agent_pod, node_name=node_name, ports=metrics_ports,
        image=scraper_image, namespace=scraper_namespace,
        timeout_s=scraper_timeout_s,
        container_name=getattr(probe, "container_name", None),
    )
    if not raw:
        return headline

    # Split off the scraper's diagnostic preamble (per-port HTTP probes).
    # On success the log looks like:
    #   === probe port=9962 ===
    #   ...
    #   === METRICS port=9962 ===
    #   <prometheus text>
    metrics_text = raw
    diag_text = ""
    sep_idx = raw.find("=== METRICS port=")
    if sep_idx >= 0:
        diag_text = raw[:sep_idx]
        nl = raw.find("\n", sep_idx)
        metrics_text = raw[nl + 1:] if nl >= 0 else ""

    iter_dir.mkdir(parents=True, exist_ok=True)
    if diag_text:
        (iter_dir / "scraper_probe.log").write_text(diag_text)

    if "cilium_" not in metrics_text:
        # No port returned metrics — keep diagnostics for inspection but bail.
        (iter_dir / "scraper_probe.log").write_text(raw)
        return headline

    (iter_dir / "cilium_metrics.txt").write_text(metrics_text)
    parsed = parse_metrics(metrics_text)
    headline["metrics"] = parsed
    if parsed.get("bootstrap"):
        headline["bootstrap"] = parsed["bootstrap"]
    if parsed.get("version"):
        headline["cilium_version"] = parsed["version"]

    # Best-effort: image tag from the agent pod is always available and gives
    # us a reliable version when `cilium_version_info` isn't emitted.
    try:
        for c in (agent_pod.spec.containers or []):
            if c.name == probe.container_name:
                headline.setdefault("cilium_version", c.image)
                break
    except Exception:  # noqa: BLE001
        pass

    (iter_dir / "cilium_deep_headline.json").write_text(
        json.dumps(headline, indent=2, default=str))
    return headline


# ---- record integration ---------------------------------------------------

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
