"""EKS provider with Cilium installed in ENI mode.

Follows the Isovalent guide
(https://cilium.io/blog/2025/06/19/eks-eni-install/):

1. Create an EKS control plane with ``eksctl --without-nodegroup``.
2. Create a small ``systempool`` managed nodegroup for system add-ons.
3. Tear down the AWS VPC CNI (``aws-node``) and (optionally) ``kube-proxy`` so
   Cilium can take over networking and act as the kube-proxy replacement.
4. Helm-install Cilium with ``eni.enabled=true`` / ``ipam.mode=eni``.
5. Create a zero-node ``latencypool`` autoscaling nodegroup; trigger pods get
   pinned here via ``nodeSelector: {nodepool: latencypool}`` so every
   iteration deterministically provisions a fresh VM.
6. Helm-install the upstream Cluster Autoscaler so the latencypool ASG scales
   from 0 in response to Pending pods.

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


class EKSEniCiliumProvider(ClusterProvider):
    name = "eks_eni_cilium"

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

        # 1. Control plane only (no default nodegroup).
        create_args = [
            "create", "cluster",
            "--name", cluster,
            "--region", region,
            "--without-nodegroup",
        ]
        if e.kubernetes_version:
            create_args += ["--version", e.kubernetes_version]
        _eks.eksctl(create_args)

        # Pull kubeconfig before we touch nodegroups / CNI.
        _eks.eks_write_kubeconfig(cluster, region, kc)

        # 2. System nodegroup (hosts kube-system + cilium-operator + cluster-autoscaler).
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

        # 3. Strip the VPC CNI so Cilium can own pod networking. We delete
        # kube-proxy too because kubeProxyReplacement=true makes it redundant
        # (and leaving it can shadow Cilium's BPF service rules).
        _eks.kubectl(["-n", "kube-system", "delete", "daemonset", "aws-node",
                      "--ignore-not-found=true"], kubeconfig=kc)
        _eks.kubectl(["-n", "kube-system", "delete", "daemonset", "kube-proxy",
                      "--ignore-not-found=true"], kubeconfig=kc)

        # 4. Cilium in ENI mode. k8sServiceHost/Port point at the EKS apiserver
        # directly so the agent can come up before kube-proxy-replacement is
        # established.
        api_host = _eks.eks_describe_cluster_endpoint(cluster, region)
        log.info("installing Cilium %s in ENI mode onto %s (k8sServiceHost=%s)",
                 e.cilium.chart_version, cluster, api_host)
        _eks.helm_repo_add("cilium", e.cilium.repo_url)
        values = dict(e.cilium.values)
        values.setdefault("k8sServiceHost", api_host)
        values.setdefault("k8sServicePort", "443")
        _eks.helm_install(
            release="cilium", chart="cilium/cilium",
            kubeconfig=kc, version=e.cilium.chart_version,
            namespace="kube-system", values=values,
            timeout_s=e.cilium.install_timeout_s,
        )
        _eks.kubectl_rollout_status(kc, kind="daemonset", name="cilium",
                                    namespace="kube-system",
                                    timeout_s=e.cilium.install_timeout_s)

        # 5. Zero-node autoscaling latencypool.
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

        # 6. Cluster Autoscaler (so the latencypool ASG actually scales from 0).
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
        return get_probe(self.cfg.cni.probe or "cilium_generic")

    def describe(self, h: ClusterHandle) -> dict:
        e = self.cfg.eks
        return {
            "kubernetes_version": e.kubernetes_version,
            "system_pool_instance_type": e.system_node_pool.instance_type,
            "user_pool_instance_type": e.user_node_pool.instance_type,
            "cilium_chart_version": e.cilium.chart_version,
            "cilium_ipam_mode": "eni",
            "kube_proxy_replacement": True,
            "cluster_autoscaler": e.cluster_autoscaler.enabled,
        }
