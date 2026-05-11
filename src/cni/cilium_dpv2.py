"""GKE Dataplane V2 (anetd) CNI probe."""
from __future__ import annotations

from .base import CNIProbe

PROBE = CNIProbe(
    name="cilium_dpv2",
    namespace="kube-system",
    label_selector="k8s-app=cilium",
    container_name="cilium-agent",
    ready_regex=r"All Cilium daemons are ready|Daemon initialization completed",
)
