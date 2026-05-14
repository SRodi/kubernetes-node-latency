"""Generic upstream Cilium CNI probe (AKS managed Cilium / BYOCNI / kind / EKS)."""
from __future__ import annotations

from .base import CNIProbe

PROBE = CNIProbe(
    name="cilium_generic",
    namespace="kube-system",
    label_selector="k8s-app=cilium",
    container_name="cilium-agent",
    ready_regex=r"All Cilium daemons are ready|Daemon initialization completed",
    use_cilium_cli=False,
    # AKS managed Cilium and Helm-installed Cilium both run with
    # `set-cilium-node-taints=true`. The operator stamps this NoSchedule
    # taint on every new node and only the local agent removes it once
    # Ready — so pod scheduling is gated on it, not on Node Ready=True.
    blocking_taint_keys=("node.cilium.io/agent-not-ready",),
)
