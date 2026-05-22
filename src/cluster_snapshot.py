"""One-shot snapshot of cluster manifests / runtime config relevant to
node-startup latency analysis. Runs once per test (after the first node
appears so we can fetch the kubelet's effective configz) and persists
artifacts under ``<run_dir>/cluster_manifests/``.

Captured artifacts:

* ``kubelet_configz_<node>.json``
      The kubelet's *effective* configuration as served by the
      ``/api/v1/nodes/<node>/proxy/configz`` endpoint. This is the only
      authoritative source for whether image pulls are serialized
      (``serializeImagePulls``) and how many parallel pulls are allowed
      (``maxParallelImagePulls``, k8s >= 1.27) — both of which directly
      explain the head-of-line wait observed between an image's
      ``Pulled`` event and the kubelet creating the container.
* ``daemonset_<namespace>__<name>.yaml``
      Every DaemonSet in ``kube-system`` plus any namespace seeded by
      Cilium. We render to YAML (not JSON) so the file matches what a
      user would see from ``kubectl get ds <name> -o yaml``.
* ``configmap_<namespace>__<name>.yaml``
      Relevant networking ConfigMaps (cilium-config, azure-cns-config,
      coredns, kube-proxy when present).

All captures are best-effort: failures are logged at WARNING and never
break the run.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml  # PyYAML is already a transitive dep of kubernetes-client.
from kubernetes import client
from kubernetes.client.rest import ApiException

log = logging.getLogger(__name__)

_NETWORKING_NAMESPACES = ("kube-system",)
_RELEVANT_CONFIGMAPS = {
    # (namespace, name) — captured if present.
    ("kube-system", "cilium-config"),
    ("kube-system", "azure-cns-config"),
    ("kube-system", "azure-ipam-config"),
    ("kube-system", "kube-proxy"),
    ("kube-system", "coredns"),
    ("kube-system", "kubelet-config"),
}


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ApiException as e:
        if e.status == 404:
            return None
        log.debug("cluster-snapshot API call failed: %s", e.reason)
        return None
    except Exception as e:  # noqa: BLE001
        log.debug("cluster-snapshot call raised: %s", e)
        return None


def _to_dict(obj) -> Any:
    """Convert a kubernetes-client object tree into plain Python dicts/lists
    suitable for YAML/JSON serialization, matching kubectl output."""
    if hasattr(obj, "to_dict"):
        return _to_dict(obj.to_dict())
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_to_dict(v) for v in obj]
    return obj


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fp:
        yaml.safe_dump(data, fp, default_flow_style=False, sort_keys=False)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fp:
        json.dump(data, fp, indent=2, default=str)


def _fetch_kubelet_configz(core: client.CoreV1Api, node: str) -> dict | None:
    """Fetch the kubelet's effective config via the apiserver proxy.

    Endpoint: ``/api/v1/nodes/<node>/proxy/configz`` — served by every
    modern kubelet (>= 1.10). Returns the parsed JSON, or None on
    failure.
    """
    api_client = core.api_client
    try:
        resp = api_client.call_api(
            f"/api/v1/nodes/{node}/proxy/configz",
            "GET",
            response_type="object",
            _return_http_data_only=True,
            _preload_content=True,
            auth_settings=["BearerToken"],
        )
    except ApiException as e:
        log.warning("kubelet configz fetch failed for %s: %s", node, e.reason)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("kubelet configz fetch raised for %s: %s", node, e)
        return None
    return resp


def snapshot(core: client.CoreV1Api, apps: client.AppsV1Api,
             *, node: str | None, out_dir: Path) -> dict:
    """Capture kubelet configz + DaemonSets + relevant ConfigMaps.

    Parameters
    ----------
    node : str | None
        Node name to fetch kubelet configz from. When None, configz is
        skipped (DS / CM captures still run).
    out_dir : Path
        Directory to write into (created if needed). Typically
        ``<run_dir>/cluster_manifests``.

    Returns
    -------
    summary : dict
        ``{kubelet_configz, daemonsets, configmaps, errors}``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"kubelet_configz": None, "daemonsets": 0, "configmaps": 0, "errors": 0}

    # --- kubelet configz -----------------------------------------------------
    if node:
        cfg = _fetch_kubelet_configz(core, node)
        if cfg is not None:
            path = out_dir / f"kubelet_configz_{node}.json"
            try:
                _write_json(path, cfg)
                summary["kubelet_configz"] = str(path.name)
                # Surface the most relevant fields in the log for quick triage.
                inner = cfg.get("kubeletconfig", cfg)
                spi = inner.get("serializeImagePulls")
                mpi = inner.get("maxParallelImagePulls")
                log.info(
                    "kubelet config: serializeImagePulls=%s maxParallelImagePulls=%s "
                    "imagePullProgressDeadline=%s imageMinimumGCAge=%s",
                    spi, mpi,
                    inner.get("imagePullProgressDeadline"),
                    inner.get("imageMinimumGCAge"),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("kubelet configz write failed: %s", e)
                summary["errors"] += 1
        else:
            summary["errors"] += 1

    # --- DaemonSets ----------------------------------------------------------
    for ns in _NETWORKING_NAMESPACES:
        resp = _safe(apps.list_namespaced_daemon_set, namespace=ns)
        if resp is None:
            summary["errors"] += 1
            continue
        for ds in resp.items or []:
            name = ds.metadata.name
            data = _to_dict(ds)
            # Strip managedFields — verbose, server-side noise that masks
            # the meaningful spec when diffing two runs.
            md = data.get("metadata") or {}
            md.pop("managed_fields", None)
            md.pop("managedFields", None)
            path = out_dir / f"daemonset_{ns}__{name}.yaml"
            try:
                _write_yaml(path, data)
                summary["daemonsets"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("DS %s/%s write failed: %s", ns, name, e)
                summary["errors"] += 1

    # --- ConfigMaps ----------------------------------------------------------
    for (ns, name) in _RELEVANT_CONFIGMAPS:
        cm = _safe(core.read_namespaced_config_map, name=name, namespace=ns)
        if cm is None:
            continue
        data = _to_dict(cm)
        md = data.get("metadata") or {}
        md.pop("managed_fields", None)
        md.pop("managedFields", None)
        path = out_dir / f"configmap_{ns}__{name}.yaml"
        try:
            _write_yaml(path, data)
            summary["configmaps"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("CM %s/%s write failed: %s", ns, name, e)
            summary["errors"] += 1

    # --- Summary index -------------------------------------------------------
    try:
        _write_json(out_dir / "_index.json", summary)
    except Exception:  # noqa: BLE001
        pass
    return summary
