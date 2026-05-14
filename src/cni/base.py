"""CNIProbe abstraction.

A CNIProbe describes how to find the CNI agent pod on a freshly-provisioned
node and how to recognise the "agent ready" log line. Collectors use this
to derive T2 (agent container started) and T3 (agent ready).
"""
from __future__ import annotations

import dataclasses as dc
import re


@dc.dataclass
class CNIProbe:
    name: str
    namespace: str
    label_selector: str       # k8s label selector e.g. "k8s-app=cilium"
    container_name: str       # container within the agent pod
    ready_regex: str          # regex matched against agent log lines for T3 (fallback)
    pod_ready_condition: str = "Ready"  # primary T3 signal: pod condition type
    use_cilium_cli: bool = False  # fallback / alternative
    # When true, the runner and metadata skip all agent-pod lookups: there is
    # no per-node CNI agent to probe (e.g. AKS kubenet, where kubelet handles
    # the bridge + host-local IPAM in-process and kube-proxy provides
    # service routing). T2/T3 are emitted as null; T0/T1/T4 still measured.
    skip: bool = False
    # NoSchedule taints applied by the CNI's operator to new nodes that
    # block workload pod binding until the local agent clears them. On AKS
    # managed Cilium / BYOCNI Cilium this includes
    # `node.cilium.io/agent-not-ready` (see the ConfigMap keys
    # `set-cilium-node-taints` / `remove-cilium-node-taints`). On GKE DPv2
    # (anetd) and AKS kubenet no such operator taint is configured, so this
    # tuple is empty and `cilium_scheduling_block_s` collapses to 0.
    blocking_taint_keys: tuple[str, ...] = ()

    def ready_pattern(self) -> re.Pattern[str]:
        return re.compile(self.ready_regex)
