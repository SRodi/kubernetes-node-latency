"""No-op CNI probe for providers without a per-node CNI agent Pod.

Used by AKS kubenet, where the dataplane is the kubelet-managed bridge
plus host-local IPAM and kube-proxy iptables — there is no DaemonSet
whose Pod-Ready transition corresponds to "dataplane up on this node".

With this probe the runner records T0/T1/T4 (primary KPI unaffected)
and leaves T2/T3 as null. Derived metrics that depend on them
(`cilium_init_duration_s`, `cni_induced_delay_s`) are therefore null.
"""
from __future__ import annotations

from .base import CNIProbe

PROBE = CNIProbe(
    name="noop",
    namespace="kube-system",
    label_selector="",
    container_name="",
    ready_regex=r"$^",  # never matches
    skip=True,
)
