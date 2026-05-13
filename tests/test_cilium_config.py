"""Tests for the one-shot Cilium configuration snapshot."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from src import cilium_config


def _cm(data: dict[str, str]):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="cilium-config", namespace="kube-system"),
        data=data,
    )


def _ds(name: str = "anetd"):
    container = SimpleNamespace(
        name="cilium-agent", image="anetd:v1.18.6",
        args=["--config-dir=/tmp"], command=None,
        ports=[SimpleNamespace(name="agent-health", container_port=9879,
                                protocol="TCP")],
        env=[SimpleNamespace(name="K8S_NODE_NAME"),
              SimpleNamespace(name="CILIUM_K8S_NAMESPACE")],
        resources=SimpleNamespace(to_dict=lambda: {"requests": {"cpu": "100m"}}),
    )
    template = SimpleNamespace(spec=SimpleNamespace(
        containers=[container], service_account_name="anetd",
        host_network=True, host_pid=False, priority_class_name="system-node-critical"))
    template.to_dict = lambda: {"name": name}
    ds = SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="kube-system"),
        spec=SimpleNamespace(template=template),
    )
    ds.to_dict = lambda: {"name": name, "containers": ["cilium-agent"]}
    return ds


def _op_pod(name: str = "cilium-operator-xyz"):
    p = SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="kube-system",
                                  labels={"io.cilium/app": "operator"}),
        spec=SimpleNamespace(),
        status=SimpleNamespace(phase="Running"),
    )
    p.to_dict = lambda: {"name": name}
    return p


def _op_dep(name: str = "cilium-operator"):
    container = SimpleNamespace(
        name="cilium-operator", image="cilium-operator:v1.18.6",
        args=["--config-dir=/tmp/cilium/config-map"], command=None,
        ports=[], env=[], resources=None,
    )
    template = SimpleNamespace(spec=SimpleNamespace(
        containers=[container], service_account_name="cilium-operator",
        host_network=False, host_pid=False, priority_class_name=None))
    template.to_dict = lambda: {}
    d = SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="kube-system"),
        spec=SimpleNamespace(template=template),
    )
    d.to_dict = lambda: {"name": name}
    return d


def _list(items):
    return SimpleNamespace(items=items)


def _api_404(*_a, **_kw):
    raise ApiException(status=404, reason="Not Found")


def test_snapshot_writes_all_artifacts(tmp_path):
    core, apps = MagicMock(), MagicMock()
    cm_data = {
        "ipam": "kubernetes",
        "kube-proxy-replacement": "true",
        "routing-mode": "tunnel",
        "prometheus-serve-addr": ":9990",
        "operator-prometheus-serve-addr": ":6942",
    }
    core.read_namespaced_config_map.return_value = _cm(cm_data)
    core.list_namespaced_pod.return_value = _list([_op_pod()])
    apps.list_namespaced_daemon_set.return_value = _list([_ds()])
    apps.list_namespaced_deployment.return_value = _list([_op_dep()])

    out = tmp_path / "cilium_config"
    summary = cilium_config.snapshot(
        core, apps, namespace="kube-system",
        agent_label_selector="k8s-app=cilium", out_dir=out)

    assert (out / "cilium-config.json").exists()
    assert (out / "agent_daemonset.json").exists()
    assert (out / "operator_pod.json").exists()
    assert (out / "operator_deployment.json").exists()
    assert (out / "summary.json").exists()

    written = json.loads((out / "cilium-config.json").read_text())
    assert written["data"]["ipam"] == "kubernetes"
    assert summary["configmap_highlights"]["ipam"] == "kubernetes"
    assert summary["configmap_highlights"]["prometheus-serve-addr"] == ":9990"
    assert summary["agent_daemonset"] == "anetd"
    assert summary["operator_deployment"] == "cilium-operator"
    assert summary["agent_template"]["host_network"] is True
    assert summary["agent_template"]["containers"][0]["ports"][0]["container_port"] == 9879


def test_snapshot_handles_missing_configmap_and_operator(tmp_path):
    core, apps = MagicMock(), MagicMock()
    core.read_namespaced_config_map.side_effect = _api_404
    # No operator pod / deployment match any selector.
    core.list_namespaced_pod.return_value = _list([])
    apps.list_namespaced_deployment.return_value = _list([])
    # Agent DaemonSet still present.
    apps.list_namespaced_daemon_set.return_value = _list([_ds()])

    out = tmp_path / "cilium_config"
    summary = cilium_config.snapshot(
        core, apps, namespace="kube-system",
        agent_label_selector="k8s-app=cilium", out_dir=out)

    assert "configmap" not in summary
    assert "operator_pod" not in summary
    assert "operator_deployment" not in summary
    assert summary["agent_daemonset"] == "anetd"
    assert (out / "agent_daemonset.json").exists()
    assert not (out / "cilium-config.json").exists()


def test_snapshot_operator_label_fallback(tmp_path):
    """Operator labelled `name=cilium-operator` (older charts) should still
    be discovered after the primary `io.cilium/app=operator` selector misses.
    """
    core, apps = MagicMock(), MagicMock()
    core.read_namespaced_config_map.return_value = _cm({})

    def list_pod(_ns, label_selector="", **_):
        if label_selector == "name=cilium-operator":
            return _list([_op_pod()])
        return _list([])

    def list_dep(_ns, label_selector="", **_):
        if label_selector == "name=cilium-operator":
            return _list([_op_dep()])
        return _list([])

    core.list_namespaced_pod.side_effect = list_pod
    apps.list_namespaced_deployment.side_effect = list_dep
    apps.list_namespaced_daemon_set.return_value = _list([_ds()])

    out = tmp_path / "cilium_config"
    summary = cilium_config.snapshot(
        core, apps, namespace="kube-system",
        agent_label_selector="k8s-app=cilium", out_dir=out)
    assert summary["operator_label_selector"] == "name=cilium-operator"
    assert summary["operator_deployment"] == "cilium-operator"
