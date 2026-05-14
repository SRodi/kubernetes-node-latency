"""GKE Dataplane V2 (anetd) CNI probe."""
from __future__ import annotations

from .base import CNIProbe

PROBE = CNIProbe(
    name="cilium_dpv2",
    namespace="kube-system",
    label_selector="k8s-app=cilium",
    container_name="cilium-agent",
    ready_regex=r"All Cilium daemons are ready|Daemon initialization completed",
    # GKE DPv2 / anetd does NOT enable `set-cilium-node-taints` — the
    # CNI config is pre-installed on the COS node image, so pod scheduling
    # is gated only by Node Ready=True. Leaving blocking_taint_keys empty.
    blocking_taint_keys=(),
)
