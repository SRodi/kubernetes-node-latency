"""AWS VPC CNI probe (the default `aws-node` DaemonSet on EKS).

Used by the ``eks_vpc_cni`` provider, which keeps the stock Amazon VPC CNI
(``amazon-vpc-cni-k8s``) instead of replacing it with Cilium. The per-node
agent is the ``aws-node`` Pod; its ``aws-node`` container reports T2 and the
Pod-Ready transition reports T3.

Unlike Cilium, the VPC CNI does not stamp an ``agent-not-ready`` NoSchedule
taint on new nodes, so ``blocking_taint_keys`` is empty and
``cilium_scheduling_block_s`` collapses to 0.
"""
from __future__ import annotations

from .base import CNIProbe

PROBE = CNIProbe(
    name="aws_vpc",
    namespace="kube-system",
    label_selector="k8s-app=aws-node",
    container_name="aws-node",
    # Primary T3 signal is the Pod-Ready condition; this regex is only a
    # fallback. ipamd logs this once the gRPC server is up and serving.
    ready_regex=r"Serving RPC|Successfully copied CNI plugin binary",
    use_cilium_cli=False,
    blocking_taint_keys=(),
)
