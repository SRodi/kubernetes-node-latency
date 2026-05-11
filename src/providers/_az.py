"""Shared Azure / Helm helpers for AKS providers."""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _run(cmd: list[str], *, env: dict | None = None, capture: bool = False,
         check: bool = True) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, env=env, text=True, capture_output=capture, check=check)


def az(args: list[str], *, json_output: bool = False, env: dict | None = None,
       check: bool = True):
    cmd = ["az", *args]
    if json_output:
        cmd += ["--output", "json"]
    res = _run(cmd, env=env, capture=True, check=check)
    if json_output and res.stdout.strip():
        return json.loads(res.stdout)
    return res


def helm(args: list[str], *, env: dict | None = None, check: bool = True):
    return _run(["helm", *args], env=env, check=check)


def kubectl(args: list[str], *, kubeconfig: Path | str, env: dict | None = None,
            check: bool = True, capture: bool = False):
    e = dict(env or os.environ)
    e["KUBECONFIG"] = str(kubeconfig)
    return _run(["kubectl", *args], env=e, check=check, capture=capture)


def ensure_resource_group(name: str, location: str) -> None:
    az(["group", "create", "--name", name, "--location", location])


def aks_get_credentials(rg: str, cluster: str, kubeconfig: Path) -> Path:
    az(["aks", "get-credentials", "-g", rg, "-n", cluster,
        "--file", str(kubeconfig), "--overwrite-existing"])
    return kubeconfig


def aks_delete(rg: str, cluster: str, *, no_wait: bool = True) -> None:
    cmd = ["aks", "delete", "-g", rg, "-n", cluster, "--yes"]
    if no_wait:
        cmd.append("--no-wait")
    az(cmd, check=False)


def aks_nodepool_scale(rg: str, cluster: str, pool: str, *, count: int) -> None:
    az(["aks", "nodepool", "scale", "-g", rg, "--cluster-name", cluster,
        "-n", pool, "--node-count", str(count)])


def helm_repo_add(name: str, url: str) -> None:
    helm(["repo", "add", name, url], check=False)
    helm(["repo", "update"])


def helm_install_cilium(*, kubeconfig: Path, version: str, values: dict,
                        namespace: str = "kube-system",
                        release: str = "cilium",
                        timeout_s: int = 600) -> None:
    set_args: list[str] = []
    for k, v in values.items():
        set_args += ["--set", f"{k}={v}"]
    cmd = ["upgrade", "--install", release, "cilium/cilium",
           "--version", version,
           "--namespace", namespace,
           "--kubeconfig", str(kubeconfig),
           "--wait", "--timeout", f"{timeout_s}s",
           *set_args]
    helm(cmd)


def kubectl_rollout_status(kubeconfig: Path, *, kind: str, name: str,
                            namespace: str, timeout_s: int = 600) -> None:
    kubectl(["rollout", "status", f"{kind}/{name}", "-n", namespace,
             f"--timeout={timeout_s}s"], kubeconfig=kubeconfig)
