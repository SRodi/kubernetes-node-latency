"""One-shot Cilium configuration snapshot — runs once per test, before
the iteration loop, and persists the static configuration of the agent
and operator under `<run_dir>/cilium_config/`.

This is independent of `--deep-cilium` (which captures runtime metrics
per iteration). Configuration is invariant during a run, so it is
captured exactly once and is essential for comparing two runs that
may differ in agent flags, image tags, IPAM mode, kube-proxy-replacement
mode, etc.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

log = logging.getLogger(__name__)

# Operator labels vary per platform; we try them in order until one matches.
_OPERATOR_LABEL_SELECTORS = (
    "io.cilium/app=operator",
    "name=cilium-operator",
    "k8s-app=cilium-operator",
    "app.kubernetes.io/name=cilium-operator",
)
# Likely ConfigMap names to look for. `cilium-config` is the canonical one
# on every distribution we've encountered (GKE anetd included).
_CONFIGMAP_NAMES = ("cilium-config",)


def _safe(fn, *args, **kwargs):
    """Call a kubernetes-client API; return None on 404, re-raise others."""
    try:
        return fn(*args, **kwargs)
    except ApiException as e:
        if e.status == 404:
            return None
        log.debug("config-snapshot API call failed: %s", e.reason)
        return None
    except Exception as e:  # noqa: BLE001
        log.debug("config-snapshot call raised: %s", e)
        return None


def _to_dict(obj) -> Any:
    """Best-effort dict serialization for kubernetes-client objects."""
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def _find_operator_pod(core: client.CoreV1Api, namespace: str):
    for sel in _OPERATOR_LABEL_SELECTORS:
        pods = _safe(core.list_namespaced_pod, namespace,
                     label_selector=sel, limit=1)
        if pods and pods.items:
            return pods.items[0], sel
    return None, None


def _find_agent_daemonset(apps: client.AppsV1Api, namespace: str,
                            label_selector: str):
    dss = _safe(apps.list_namespaced_daemon_set, namespace,
                 label_selector=label_selector, limit=1)
    if dss and dss.items:
        return dss.items[0]
    return None


def _find_operator_deployment(apps: client.AppsV1Api, namespace: str):
    for sel in _OPERATOR_LABEL_SELECTORS:
        deps = _safe(apps.list_namespaced_deployment, namespace,
                      label_selector=sel, limit=1)
        if deps and deps.items:
            return deps.items[0]
    return None


def _summarize_pod_template(spec) -> dict:
    """Pull the high-signal bits out of a PodTemplateSpec."""
    if spec is None:
        return {}
    containers = []
    for c in (spec.spec.containers or []):
        containers.append({
            "name": c.name,
            "image": c.image,
            "args": list(c.args or []),
            "command": list(c.command or []),
            "ports": [{"name": p.name, "container_port": p.container_port,
                        "protocol": p.protocol} for p in (c.ports or [])],
            "env_keys": sorted([e.name for e in (c.env or []) if e.name]),
            "resources": _to_dict(c.resources),
        })
    return {
        "service_account": spec.spec.service_account_name,
        "host_network": bool(spec.spec.host_network),
        "host_pid": bool(spec.spec.host_pid),
        "priority_class": spec.spec.priority_class_name,
        "containers": containers,
    }


def snapshot(core: client.CoreV1Api, apps: client.AppsV1Api, *,
              namespace: str, agent_label_selector: str,
              out_dir: Path) -> dict:
    """Write Cilium configuration artefacts to `out_dir` and return a
    small summary dict suitable for embedding in `run_metadata.json`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"namespace": namespace}

    # --- ConfigMap (cilium-config) ---------------------------------------
    cm_data: dict[str, str] | None = None
    for name in _CONFIGMAP_NAMES:
        cm = _safe(core.read_namespaced_config_map, name, namespace)
        if cm is not None:
            cm_data = dict(cm.data or {})
            (out_dir / f"{name}.json").write_text(
                json.dumps({"name": name, "namespace": namespace,
                            "data": cm_data}, indent=2, sort_keys=True))
            summary["configmap"] = name
            break
    if cm_data:
        # Headline knobs that materially affect node-startup / dataplane
        # behaviour. Missing keys are reported as null.
        summary["configmap_highlights"] = {
            k: cm_data.get(k) for k in (
                "ipam", "routing-mode", "kube-proxy-replacement",
                "enable-ipv4", "enable-ipv6", "enable-bpf-masquerade",
                "enable-host-legacy-routing", "enable-endpoint-routes",
                "enable-bandwidth-manager", "enable-local-redirect-policy",
                "enable-cilium-endpoint-slice", "identity-allocation-mode",
                "identity-management-mode", "cni-chaining-mode",
                "prometheus-serve-addr", "operator-prometheus-serve-addr",
                "agent-health-port", "monitor-aggregation",
                "bpf-lb-service-map-max", "bpf-policy-map-max",
            )
        }

    # --- Agent DaemonSet --------------------------------------------------
    ds = _find_agent_daemonset(apps, namespace, agent_label_selector)
    if ds is not None:
        (out_dir / "agent_daemonset.json").write_text(
            json.dumps(_to_dict(ds), indent=2, default=str, sort_keys=True))
        summary["agent_daemonset"] = ds.metadata.name
        summary["agent_template"] = _summarize_pod_template(ds.spec.template)

    # --- Operator Pod + Deployment ---------------------------------------
    op_pod, op_sel = _find_operator_pod(core, namespace)
    if op_pod is not None:
        (out_dir / "operator_pod.json").write_text(
            json.dumps(_to_dict(op_pod), indent=2, default=str, sort_keys=True))
        summary["operator_pod"] = op_pod.metadata.name
        summary["operator_label_selector"] = op_sel

    op_dep = _find_operator_deployment(apps, namespace)
    if op_dep is not None:
        (out_dir / "operator_deployment.json").write_text(
            json.dumps(_to_dict(op_dep), indent=2, default=str, sort_keys=True))
        summary["operator_deployment"] = op_dep.metadata.name
        summary["operator_template"] = _summarize_pod_template(
            op_dep.spec.template)

    # --- Concise summary --------------------------------------------------
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str, sort_keys=True))
    return summary
