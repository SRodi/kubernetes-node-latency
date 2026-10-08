"""Shared helper for summarizing a live `V1Node` object into a small,
JSON-serializable dict. Used by both `metadata.py` (one-shot pre-run
cluster snapshot) and `collectors.py` (per-iteration ground-truth lookup
of the actual node a trigger pod landed on).
"""
from __future__ import annotations

from typing import Any

INTERESTING_NODE_LABELS = (
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


def node_summary(n) -> dict[str, Any]:
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
        "labels": {k: v for k, v in labels.items() if k in INTERESTING_NODE_LABELS},
    }
