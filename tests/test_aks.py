"""Mocked unit tests for AKS providers."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.config import Config
from src.providers.aks_overlay_cilium import AKSOverlayCiliumProvider
from src.providers.aks_byocni import AKSBYOCNIProvider
from src.providers.aks_kubenet import AKSKubenetProvider


def _cfg(mode: str = "cluster_autoscaler", provider: str = "aks_overlay_cilium") -> Config:
    return Config.from_dict({
        "provider": provider,
        "region": "westeurope",
        "cluster_name": "nlt",
        "aks": {
            "resource_group": "rg-nlt",
            "node_provisioning": mode,
            "system_node_pool": {"name": "sys", "vm_size": "Standard_D2s_v5", "node_count": 1},
            "user_node_pool": {"name": "userpool", "vm_size": "Standard_D4s_v5",
                                "min_count": 0, "max_count": 5, "node_count": 0},
        },
    })


def _flatten_az_calls(mock):
    calls = []
    for c in mock.call_args_list:
        args = c.args[0] if c.args else c.kwargs.get("args", [])
        if args and args[0] == "az":
            calls.append(args)
        else:
            calls.append(["az", *args])
    return calls


def test_overlay_cilium_create_args_managed_dataplane():
    cfg = _cfg(mode="cluster_autoscaler", provider="aks_overlay_cilium")
    p = AKSOverlayCiliumProvider(cfg)
    with patch("src.providers._az._run") as run:
        run.return_value = MagicMock(stdout="", returncode=0)
        p.create(cfg)
    cmds = [c.args[0] for c in run.call_args_list]
    create_cmd = next(c for c in cmds if c[:3] == ["az", "aks", "create"])
    assert "--network-plugin" in create_cmd and create_cmd[create_cmd.index("--network-plugin") + 1] == "azure"
    assert "--network-dataplane" in create_cmd and create_cmd[create_cmd.index("--network-dataplane") + 1] == "cilium"
    assert "--network-plugin-mode" in create_cmd
    np_cmd = next(c for c in cmds if c[:4] == ["az", "aks", "nodepool", "add"])
    assert "--enable-cluster-autoscaler" in np_cmd


def test_byocni_invokes_helm_install():
    cfg = _cfg(mode="cluster_autoscaler", provider="aks_byocni")
    p = AKSBYOCNIProvider(cfg)
    with patch("src.providers._az._run") as run:
        run.return_value = MagicMock(stdout="", returncode=0)
        p.create(cfg)
    cmds = [c.args[0] for c in run.call_args_list]
    create_cmd = next(c for c in cmds if c[:3] == ["az", "aks", "create"])
    assert "--network-plugin" in create_cmd and create_cmd[create_cmd.index("--network-plugin") + 1] == "none"
    assert any(c[0] == "helm" and "upgrade" in c for c in cmds)
    helm_cmd = next(c for c in cmds if c[0] == "helm" and "upgrade" in c)
    assert "cilium/cilium" in helm_cmd
    assert "--version" in helm_cmd


def test_nap_mode_skips_user_nodepool_and_sets_provisioning_auto():
    cfg = _cfg(mode="nap", provider="aks_overlay_cilium")
    p = AKSOverlayCiliumProvider(cfg)
    with patch("src.providers._az._run") as run:
        run.return_value = MagicMock(stdout="", returncode=0)
        p.create(cfg)
    cmds = [c.args[0] for c in run.call_args_list]
    create_cmd = next(c for c in cmds if c[:3] == ["az", "aks", "create"])
    assert "--node-provisioning-mode" in create_cmd
    assert not any(c[:4] == ["az", "aks", "nodepool", "add"] for c in cmds)


def test_manual_mode_scales_nodepool_in_pre_iteration():
    cfg = _cfg(mode="manual", provider="aks_overlay_cilium")
    p = AKSOverlayCiliumProvider(cfg)
    with patch("src.providers._az._run") as run:
        run.return_value = MagicMock(stdout="", returncode=0)
        h = p.create(cfg)
        run.reset_mock()
        p.pre_iteration(h, 1)
        p.post_iteration(h, 1)
    cmds = [c.args[0] for c in run.call_args_list]
    scales = [c for c in cmds if c[:4] == ["az", "aks", "nodepool", "scale"]]
    assert len(scales) == 2
    assert "--node-count" in scales[0] and scales[0][scales[0].index("--node-count") + 1] == "1"
    assert "--node-count" in scales[1] and scales[1][scales[1].index("--node-count") + 1] == "0"


def test_node_autoprovision_hint_targets_user_pool():
    cfg = _cfg(mode="cluster_autoscaler", provider="aks_overlay_cilium")
    hint = AKSOverlayCiliumProvider(cfg).node_autoprovision_hint()
    assert hint["nodeSelector"]["agentpool"] == "userpool"


def test_invalid_node_provisioning_mode_raises():
    with pytest.raises(ValueError):
        AKSOverlayCiliumProvider(_cfg(mode="bogus"))


def test_kubenet_create_args_and_noop_probe():
    cfg = _cfg(mode="cluster_autoscaler", provider="aks_kubenet")
    p = AKSKubenetProvider(cfg)
    with patch("src.providers._az._run") as run:
        run.return_value = MagicMock(stdout="", returncode=0)
        p.create(cfg)
    cmds = [c.args[0] for c in run.call_args_list]
    create_cmd = next(c for c in cmds if c[:3] == ["az", "aks", "create"])
    assert "--network-plugin" in create_cmd
    assert create_cmd[create_cmd.index("--network-plugin") + 1] == "kubenet"
    # kubenet must not carry Cilium/overlay flags
    assert "--network-dataplane" not in create_cmd
    assert "--network-plugin-mode" not in create_cmd
    # Default probe is the no-op (no per-node CNI agent on kubenet)
    probe = p.cni_probe()
    assert probe.name == "noop"
    assert probe.skip is True
    # Network facts reflect kubenet, not Cilium
    assert p._network_describe()["network_plugin"] == "kubenet"
    assert p._network_describe()["cni_agent"] is None

