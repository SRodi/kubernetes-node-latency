"""Snapshot of run identity, effective config, and cluster facts.

Written once per run to `<run_dir>/run_metadata.json` so that aggregated
results (CSV, plots, summary.md) can later be attributed to a specific
provider / region / VM size / Kubernetes version / CNI version without
relying on shell history or filename conventions.
"""
from __future__ import annotations

import dataclasses as dc
import datetime as dt
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kubernetes import client

log = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _git_commit() -> str | None:
    if not shutil.which("git"):
        return None
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                           text=True, check=False, timeout=3)
        out = r.stdout.strip()
        return out or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _tool_version(tool: str, args: list[str]) -> str | None:
    if not shutil.which(tool):
        return None
    try:
        r = subprocess.run([tool, *args], capture_output=True, text=True,
                           check=False, timeout=10)
        return (r.stdout or r.stderr or "").strip().splitlines()[0] if (r.stdout or r.stderr) else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _tooling_versions() -> dict[str, str | None]:
    return {
        "kubectl": _tool_version("kubectl", ["version", "--client=true", "-o", "yaml"]),
        "gcloud": _tool_version("gcloud", ["--version"]),
        "az": _tool_version("az", ["version", "--output", "tsv"]),
        "helm": _tool_version("helm", ["version", "--short"]),
        "cilium": _tool_version("cilium", ["version", "--client"]),
    }


_INTERESTING_NODE_LABELS = (
    # GKE / GCE
    "cloud.google.com/gke-nodepool",
    "node.kubernetes.io/instance-type",
    "topology.kubernetes.io/region",
    "topology.kubernetes.io/zone",
    # AKS / Azure
    "agentpool",
    "kubernetes.azure.com/agentpool",
    "kubernetes.azure.com/mode",
    "kubernetes.azure.com/cluster",
)


def _node_summary(n) -> dict[str, Any]:
    info = n.status.node_info or None
    labels = n.metadata.labels or {}
    return {
        "name": n.metadata.name,
        "creation_timestamp": n.metadata.creation_timestamp.isoformat() if n.metadata.creation_timestamp else None,
        "kubelet_version": getattr(info, "kubelet_version", None),
        "container_runtime_version": getattr(info, "container_runtime_version", None),
        "os_image": getattr(info, "os_image", None),
        "kernel_version": getattr(info, "kernel_version", None),
        "architecture": getattr(info, "architecture", None),
        "labels": {k: v for k, v in labels.items() if k in _INTERESTING_NODE_LABELS},
    }


def _cni_image(core: client.CoreV1Api, probe) -> dict[str, str | None]:
    try:
        pods = core.list_namespaced_pod(
            probe.namespace, label_selector=probe.label_selector, limit=1).items
    except client.ApiException as e:
        log.warning("cni image lookup failed: %s", e)
        return {"image": None, "container": probe.container_name}
    if not pods:
        return {"image": None, "container": probe.container_name}
    pod = pods[0]
    for c in (pod.spec.containers or []):
        if c.name == probe.container_name:
            return {"image": c.image, "container": c.name}
    # Fall back to first container
    if pod.spec.containers:
        c = pod.spec.containers[0]
        return {"image": c.image, "container": c.name}
    return {"image": None, "container": probe.container_name}


def _cluster_facts(core: client.CoreV1Api, probe, max_nodes: int = 5) -> dict[str, Any]:
    try:
        nodes = core.list_node().items
    except client.ApiException as e:
        log.warning("cluster facts lookup failed: %s", e)
        return {"node_count_at_start": None, "nodes": [], "cni": None}
    sample = nodes[:max_nodes]
    return {
        "node_count_at_start": len(nodes),
        "kubernetes_version": (sample[0].status.node_info.kubelet_version if sample else None),
        "nodes": [_node_summary(n) for n in sample],
        "cni": _cni_image(core, probe),
    }


def _config_to_dict(cfg) -> dict[str, Any]:
    d = dc.asdict(cfg)
    # Stringify Paths for JSON serialisability.
    for k, v in list(d.items()):
        if isinstance(v, Path):
            d[k] = str(v)
    return d


def gather_metadata(*, cfg, handle, provider, core: client.CoreV1Api,
                     run_id: str, cli_argv: list[str]) -> dict[str, Any]:
    describe = getattr(provider, "describe", lambda h: {})
    return {
        "run_id": run_id,
        "schema_version": 1,
        "start_time": _utcnow_iso(),
        "end_time": None,
        "duration_s": None,
        "status": "running",
        "cli_argv": cli_argv,
        "harness_git_commit": _git_commit(),
        "tooling_versions": _tooling_versions(),
        "config": _config_to_dict(cfg),
        "cluster": {
            "provider": provider.name,
            "region": handle.region,
            "name": handle.name,
            "created_by_harness": handle.created,
            "kubeconfig": str(handle.kubeconfig),
            "extra": handle.extra,
            **_cluster_facts(core, provider.cni_probe()),
        },
        "provider_describe": describe(handle) or {},
    }


def write_metadata(run_dir: Path, meta: dict[str, Any]) -> Path:
    out = run_dir / "run_metadata.json"
    out.write_text(json.dumps(meta, indent=2, default=str, sort_keys=False))
    return out


def finalize_metadata(run_dir: Path, *, status: str) -> dict[str, Any] | None:
    out = run_dir / "run_metadata.json"
    if not out.exists():
        return None
    meta = json.loads(out.read_text())
    end = dt.datetime.now(dt.timezone.utc)
    start = dt.datetime.fromisoformat(meta["start_time"])
    meta["end_time"] = end.isoformat(timespec="seconds")
    meta["duration_s"] = round((end - start).total_seconds(), 1)
    meta["status"] = status
    out.write_text(json.dumps(meta, indent=2, default=str, sort_keys=False))
    return meta


def append_summary_section(summary_md: Path, meta: dict[str, Any]) -> None:
    if not summary_md.exists():
        return
    cluster = meta.get("cluster", {})
    cni = (cluster.get("cni") or {})
    nodes = cluster.get("nodes") or []
    machine = None
    if nodes:
        machine = (nodes[0].get("labels") or {}).get("node.kubernetes.io/instance-type")
    pdesc = meta.get("provider_describe") or {}
    lines = [
        "",
        "## Cluster",
        "",
        f"- Provider: **{cluster.get('provider')}** (`{cluster.get('name')}` @ `{cluster.get('region')}`)",
        f"- Kubernetes: `{cluster.get('kubernetes_version')}`",
        f"- Machine type: `{machine}`",
        f"- CNI image: `{cni.get('image')}`",
        f"- Nodes at start: {cluster.get('node_count_at_start')}",
    ]
    if pdesc:
        lines.append("- Provider details: " + ", ".join(f"`{k}={v}`" for k, v in pdesc.items()))
    summary_md.write_text(summary_md.read_text() + "\n".join(lines) + "\n")
