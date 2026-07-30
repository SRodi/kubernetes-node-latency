"""Registration + probe wiring for the EKS providers (aws_vpc / cilium)."""
from __future__ import annotations

import pytest

from src import providers
from src.cni import get as get_probe
from src.config import Config


def test_eks_vpc_cni_provider_registered():
    cfg = Config()
    p = providers.get("eks_vpc_cni", cfg)
    assert p.name == "eks_vpc_cni"


def test_eks_vpc_cni_uses_aws_vpc_probe():
    cfg = Config()
    p = providers.get("eks_vpc_cni", cfg)
    probe = p.cni_probe()
    assert probe.name == "aws_vpc"
    assert probe.label_selector == "k8s-app=aws-node"
    assert probe.container_name == "aws-node"
    # The VPC CNI stamps no agent-not-ready taint on new nodes.
    assert probe.blocking_taint_keys == ()
    assert probe.skip is False


def test_aws_vpc_probe_registered():
    probe = get_probe("aws_vpc")
    assert probe.container_name == "aws-node"


def test_eks_vpc_cni_autoprovision_pins_latencypool():
    cfg = Config()
    p = providers.get("eks_vpc_cni", cfg)
    hint = p.node_autoprovision_hint()
    assert hint["nodeSelector"] == {"nodepool": cfg.eks.user_node_pool.name}


def test_eks_vpc_cni_describe_reports_default_dataplane():
    cfg = Config()
    p = providers.get("eks_vpc_cni", cfg)
    d = p.describe(object())
    assert d["cni"] == "aws-vpc-cni"
    assert d["kube_proxy_replacement"] is False


def test_report_maps_have_eks_vpc_cni():
    from src.report import PROVIDER_DISPLAY_NAMES, REPORT_PODS_BY_PROVIDER

    assert PROVIDER_DISPLAY_NAMES["eks_vpc_cni"] == "EKS VPC CNI"
    assert REPORT_PODS_BY_PROVIDER["eks_vpc_cni"] == {"aws-node"}
