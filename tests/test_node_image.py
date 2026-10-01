"""Node OS image selection (image_type / os_sku / ami_family) + summary capture."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config import Config
from src.metadata import append_summary_section
from src.providers.eks_vpc_cni import EKSVPCCNIProvider
from src.providers.gke_standard_dpv2 import GKEStandardDPv2Provider


# ---- GKE Standard: --image-type ----

def _gke_cfg(image_type=None) -> Config:
    cfg = Config.from_dict({
        "provider": "gke_standard_dpv2",
        "region": "europe-west1",
        "cluster_name": "nlt",
        "gke_standard": ({"image_type": image_type} if image_type else {}),
    })
    return cfg


def test_gke_image_type_in_cluster_and_trigger_pool_when_set():
    cfg = _gke_cfg(image_type="UBUNTU_CONTAINERD")
    p = GKEStandardDPv2Provider(cfg)
    create = p._gcloud_create_args(cfg)
    assert create[create.index("--image-type") + 1] == "UBUNTU_CONTAINERD"

    h = MagicMock(name="handle", region="europe-west1")
    with patch("src.providers.gke_standard_dpv2.run") as run:
        p._post_create(h)
    np_args = run.call_args_list[0].args[0]
    assert np_args[np_args.index("--image-type") + 1] == "UBUNTU_CONTAINERD"
    assert p._describe_extra()["image_type"] == "UBUNTU_CONTAINERD"


def test_gke_image_type_absent_by_default():
    cfg = _gke_cfg()
    p = GKEStandardDPv2Provider(cfg)
    assert "--image-type" not in p._gcloud_create_args(cfg)
    h = MagicMock(name="handle", region="europe-west1")
    with patch("src.providers.gke_standard_dpv2.run") as run:
        p._post_create(h)
    assert "--image-type" not in run.call_args_list[0].args[0]


# ---- EKS: --node-ami-family ----

def _eks_cfg(ami_family=None) -> Config:
    return Config.from_dict({
        "provider": "eks_vpc_cni",
        "region": "us-east-1",
        "cluster_name": "nlt",
        "eks": ({"ami_family": ami_family} if ami_family else {}),
    })


def _eksctl_calls(mock) -> list[list[str]]:
    return [c.args[0] for c in mock.eksctl.call_args_list]


def test_eks_ami_family_in_both_nodegroups_when_set():
    cfg = _eks_cfg(ami_family="Ubuntu2204")
    p = EKSVPCCNIProvider(cfg)
    with patch("src.providers.eks_vpc_cni._eks") as m:
        m.eks_find_nodegroup_asg.return_value = None
        p.create(cfg)
    nodegroup_cmds = [c for c in _eksctl_calls(m) if c[:2] == ["create", "nodegroup"]]
    assert len(nodegroup_cmds) == 2
    for c in nodegroup_cmds:
        assert c[c.index("--node-ami-family") + 1] == "Ubuntu2204"
    assert p.describe(MagicMock())["ami_family"] == "Ubuntu2204"


def test_eks_ami_family_absent_by_default():
    cfg = _eks_cfg()
    p = EKSVPCCNIProvider(cfg)
    with patch("src.providers.eks_vpc_cni._eks") as m:
        m.eks_find_nodegroup_asg.return_value = None
        p.create(cfg)
    assert all("--node-ami-family" not in c for c in _eksctl_calls(m))


# ---- Summary: node image captured ----

def test_summary_includes_node_image(tmp_path: Path):
    summary = tmp_path / "summary.md"
    summary.write_text("# Run foo\n")
    meta = {
        "cluster": {
            "provider": "aks_overlay_cilium",
            "name": "nlt", "region": "westeurope",
            "kubernetes_version": "1.30.4",
            "node_count_at_start": 2,
            "cni": {"image": "mcr.io/cilium:v1.18"},
            "nodes": [{
                "os_image": "Ubuntu 22.04.5 LTS",
                "labels": {"node.kubernetes.io/instance-type": "Standard_D8s_v5"},
            }],
        },
        "provider_describe": {"os_sku": "Ubuntu2204"},
    }
    append_summary_section(summary, meta)
    text = summary.read_text()
    assert "- Node image: `Ubuntu 22.04.5 LTS` (requested: `Ubuntu2204`)" in text
    assert "- Machine type: `Standard_D8s_v5`" in text
