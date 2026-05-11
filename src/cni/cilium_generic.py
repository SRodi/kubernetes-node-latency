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
)
