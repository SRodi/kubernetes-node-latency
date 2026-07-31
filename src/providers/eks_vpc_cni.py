"""EKS provider using the stock AWS VPC CNI (the default ``aws-node``).

This is the A/B counterpart to :mod:`eks_eni_cilium`: same cluster topology
and autoscaling story, but networking is left on the Amazon VPC CNI
(``amazon-vpc-cni-k8s``) that EKS installs by default. It exists to compare
node-networking-wiring latency of the default dataplane against Cilium in ENI
mode on an otherwise identical cluster.

Lifecycle (mirrors the Cilium provider minus the CNI swap):

1. Create an EKS control plane with ``eksctl --without-nodegroup`` (this also
   installs the managed VPC CNI / kube-proxy / CoreDNS addons).
2. Create a small ``systempool`` managed nodegroup for system add-ons.
3. Create a zero-node ``latencypool`` autoscaling nodegroup; trigger pods get
   pinned here via ``nodeSelector: {nodepool: latencypool}`` so every
   iteration deterministically provisions a fresh VM.
4. Helm-install the upstream Cluster Autoscaler so the latencypool ASG scales
   from 0 in response to Pending pods.

There is deliberately no ``aws-node``/``kube-proxy`` teardown and no Cilium
install — the whole point is to measure the default dataplane.

Requires ``eksctl``, ``aws``, ``helm`` and ``kubectl`` on PATH plus a working
AWS credential chain (env vars, ``~/.aws/credentials``, or instance/IAM SSO).
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..cni import get as get_probe
from ..cni.base import CNIProbe
from . import _eks
from .base import ClusterHandle, ClusterProvider

log = logging.getLogger(__name__)


class EKSVPCCNIProvider(ClusterProvider):
    name = "eks_vpc_cni"

    def __init__(self, cfg):
        self.cfg = cfg

    # -- ClusterProvider --
    def _kubeconfig_path(self, cluster_name: str) -> Path:
        if getattr(self.cfg, "kubeconfig_path", None):
            return Path(self.cfg.kubeconfig_path)
        return Path.cwd() / f".kubeconfig-{self.name}-{cluster_name}"

    def create(self, cfg) -> ClusterHandle:
        e = cfg.eks
        region = e.region or cfg.region
        cluster = cfg.cluster_name
        kc = self._kubeconfig_path(cluster)

        # 1. Control plane only (no default nodegroup). EKS still installs the
        # VPC CNI (aws-node), kube-proxy and CoreDNS managed add-ons.
        create_args = [
            "create", "cluster",
            "--name", cluster,
            "--region", region,
            "--without-nodegroup",
        ]
        if e.kubernetes_version:
            create_args += ["--version", e.kubernetes_version]
        _eks.eksctl(create_args)

        # Pull kubeconfig before we touch nodegroups.
        _eks.eks_write_kubeconfig(cluster, region, kc)

        # 2. System nodegroup (hosts kube-system + cluster-autoscaler).
        sys_args = [
            "create", "nodegroup",
            "--cluster", cluster,
            "--region", region,
            "--name", e.system_node_pool.name,
            "--node-type", e.system_node_pool.instance_type,
            "--nodes", str(e.system_node_pool.node_count),
            "--node-labels", f"nodepool={e.system_node_pool.name}",
            "--asg-access",  # gives instance role autoscaling perms (used by CA)
        ]
        _eks.eksctl(sys_args)

        # NB: the stock VPC CNI (aws-node) and kube-proxy are intentionally
        # left in place — this provider measures the default dataplane.

        # 3. Zero-node autoscaling latencypool.
        up = e.user_node_pool
        np_args = [
            "create", "nodegroup",
            "--cluster", cluster,
            "--region", region,
            "--name", up.name,
            "--node-type", up.instance_type,
            "--nodes", str(up.node_count),
            "--nodes-min", str(up.min_count),
            "--nodes-max", str(up.max_count),
            "--node-labels", f"nodepool={up.name}",
            "--asg-access",
        ]
        _eks.eksctl(np_args)

        # 3a. Tag the latencypool ASG with node-template hints so the
        # Cluster Autoscaler can perform scale-from-0 decisions (see the
        # eks_eni_cilium provider for the rationale — same constraint).
        asg = _eks.eks_find_nodegroup_asg(cluster, region, up.name)
        if asg:
            log.info("tagging ASG %s with node-template/label/nodepool=%s "
                     "for scale-from-0", asg, up.name)
            _eks.asg_add_node_template_tags(
                asg, region,
                labels={"nodepool": up.name},
            )
        else:
            log.warning("could not resolve ASG for nodegroup %s — "
                        "scale-from-0 may not work", up.name)

        # 4. Cluster Autoscaler (so the latencypool ASG actually scales from 0).
        ca = e.cluster_autoscaler
        if ca.enabled:
            log.info("installing cluster-autoscaler %s into kube-system",
                     ca.chart_version or "(latest)")
            _eks.helm_repo_add("autoscaler", ca.repo_url)
            ca_values = dict(ca.values)
            ca_values.setdefault("autoDiscovery.clusterName", cluster)
            ca_values.setdefault("awsRegion", region)
            _eks.helm_install(
                release="cluster-autoscaler",
                chart="autoscaler/cluster-autoscaler",
                kubeconfig=kc, version=ca.chart_version or None,
                namespace="kube-system", values=ca_values,
                timeout_s=ca.install_timeout_s,
            )
            _eks.kubectl_rollout_status(
                kc, kind="deployment",
                name="cluster-autoscaler-aws-cluster-autoscaler",
                namespace="kube-system", timeout_s=ca.install_timeout_s)

        handle = ClusterHandle(name=cluster, region=region,
                               provider=self.name, kubeconfig=kc, created=True,
                               extra={"latencypool": up.name})
        return handle

    def get_credentials(self, h: ClusterHandle) -> Path:
        return _eks.eks_write_kubeconfig(h.name, h.region, h.kubeconfig)

    def delete(self, h: ClusterHandle) -> None:
        if not h.created:
            return
        # eksctl tears down nodegroups, the EKS control plane, and the
        # CloudFormation stacks it created (VPC, IAM roles).
        _eks.eksctl_delete_cluster(h.name, h.region, wait=False)

    def node_autoprovision_hint(self) -> dict:
        # Pin trigger pods to the zero-node latencypool so the cluster
        # autoscaler is forced to scale a fresh node from 0 each iteration.
        return {"nodeSelector": {"nodepool": self.cfg.eks.user_node_pool.name},
                "tolerations": []}

    def cni_probe(self) -> CNIProbe:
        return get_probe(self.cfg.cni.probe or "aws_vpc")

    def describe(self, h: ClusterHandle) -> dict:
        e = self.cfg.eks
        return {
            "kubernetes_version": e.kubernetes_version,
            "system_pool_instance_type": e.system_node_pool.instance_type,
            "user_pool_instance_type": e.user_node_pool.instance_type,
            "cni": "aws-vpc-cni",
            "kube_proxy_replacement": False,
            "cluster_autoscaler": e.cluster_autoscaler.enabled,
        }
