"""Shared eksctl / aws / helm / kubectl helpers for the EKS provider."""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _run(cmd: list[str], *, env: dict | None = None, capture: bool = False,
         check: bool = True, timeout: float | None = 1800.0) -> subprocess.CompletedProcess:
    """Shell out with a hard upper-bound timeout (default 30 min).

    Mirrors the pattern in ``_az._run`` / ``_cli.run``: a stalled ``eksctl`` /
    ``aws`` / ``helm`` / ``kubectl`` invocation raises ``TimeoutExpired``
    instead of hanging the harness forever.
    """
    log.info("$ %s", " ".join(shlex.quote(c) for c in cmd))
    try:
        return subprocess.run(cmd, env=env, text=True,
                              capture_output=capture, check=check,
                              timeout=timeout)
    except subprocess.CalledProcessError as e:
        if capture:
            for stream_name, payload in (("stdout", e.stdout), ("stderr", e.stderr)):
                if payload and payload.strip():
                    log.error("%s %s:\n%s", cmd[0], stream_name, payload.rstrip())
        raise


def eksctl(args: list[str], *, env: dict | None = None,
           check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return _run(["eksctl", *args], env=env, check=check, capture=capture)


def aws(args: list[str], *, env: dict | None = None,
        check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return _run(["aws", *args], env=env, check=check, capture=capture)


def helm(args: list[str], *, env: dict | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    return _run(["helm", *args], env=env, check=check)


def kubectl(args: list[str], *, kubeconfig: Path | str, env: dict | None = None,
            check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    e = dict(env or os.environ)
    e["KUBECONFIG"] = str(kubeconfig)
    return _run(["kubectl", *args], env=e, check=check, capture=capture)


def helm_repo_add(name: str, url: str) -> None:
    helm(["repo", "add", name, url], check=False)
    helm(["repo", "update"])


def helm_install(*, release: str, chart: str, kubeconfig: Path, version: str | None,
                 namespace: str, values: dict, timeout_s: int = 600,
                 create_namespace: bool = False) -> None:
    set_args: list[str] = []
    for k, v in values.items():
        set_args += ["--set", f"{k}={v}"]
    cmd = ["upgrade", "--install", release, chart,
           "--namespace", namespace,
           "--kubeconfig", str(kubeconfig),
           "--wait", "--timeout", f"{timeout_s}s"]
    if version:
        cmd += ["--version", version]
    if create_namespace:
        cmd += ["--create-namespace"]
    cmd += set_args
    helm(cmd)


def kubectl_rollout_status(kubeconfig: Path, *, kind: str, name: str,
                           namespace: str, timeout_s: int = 600) -> None:
    kubectl(["rollout", "status", f"{kind}/{name}", "-n", namespace,
             f"--timeout={timeout_s}s"], kubeconfig=kubeconfig)


def eks_describe_cluster_endpoint(cluster: str, region: str) -> str:
    """Return the apiserver hostname for the cluster (used as ``k8sServiceHost``).

    Cilium with ``kubeProxyReplacement=true`` requires direct apiserver access
    instead of routing through ``kubernetes.default.svc`` (which itself is
    served by kube-proxy/Cilium). On EKS the endpoint is region-specific:
    ``https://<id>.<region>.eks.amazonaws.com``.
    """
    res = aws(["eks", "describe-cluster", "--name", cluster, "--region", region,
               "--query", "cluster.endpoint", "--output", "text"],
              capture=True)
    url = (res.stdout or "").strip()
    # strip scheme
    return url.removeprefix("https://").removeprefix("http://")


def eks_write_kubeconfig(cluster: str, region: str, kubeconfig: Path) -> Path:
    aws(["eks", "update-kubeconfig",
         "--name", cluster, "--region", region,
         "--kubeconfig", str(kubeconfig)])
    return kubeconfig


def eksctl_delete_cluster(cluster: str, region: str, *, wait: bool = False) -> None:
    cmd = ["delete", "cluster", "--name", cluster, "--region", region, "--disable-nodegroup-eviction"]
    if not wait:
        cmd.append("--wait=false")
    eksctl(cmd, check=False)


def eks_find_nodegroup_asg(cluster: str, region: str, nodegroup: str) -> str | None:
    """Return the underlying Auto Scaling Group name for an EKS managed
    nodegroup, or ``None`` if it can't be resolved.

    EKS managed nodegroups own exactly one ASG; we need its name to attach
    ``k8s.io/cluster-autoscaler/node-template/label/*`` tags so the
    Cluster Autoscaler can perform scale-from-0 decisions when a trigger
    pod uses ``nodeSelector`` to pin to this pool.
    """
    res = aws([
        "eks", "describe-nodegroup",
        "--cluster-name", cluster,
        "--region", region,
        "--nodegroup-name", nodegroup,
        "--query", "nodegroup.resources.autoScalingGroups[0].name",
        "--output", "text",
    ], capture=True, check=False)
    name = (res.stdout or "").strip()
    if not name or name == "None":
        return None
    return name


def asg_add_node_template_tags(asg_name: str, region: str,
                               labels: dict[str, str] | None = None,
                               taints: list[str] | None = None) -> None:
    """Add ``k8s.io/cluster-autoscaler/node-template/{label,taint}/*`` tags
    to an ASG so Cluster Autoscaler can scale it from 0.

    Without these tags, CA's predicate check (``pod fits on hypothetical
    future node?``) fails when the trigger pod uses ``nodeSelector`` or
    tolerations the live ASG hasn't yet produced a node for.
    """
    tags: list[str] = []
    for k, v in (labels or {}).items():
        tags.append(
            f"ResourceId={asg_name},ResourceType=auto-scaling-group,"
            f"Key=k8s.io/cluster-autoscaler/node-template/label/{k},"
            f"Value={v},PropagateAtLaunch=false"
        )
    for t in (taints or []):
        tags.append(
            f"ResourceId={asg_name},ResourceType=auto-scaling-group,"
            f"Key=k8s.io/cluster-autoscaler/node-template/taint/{t.split('=', 1)[0]},"
            f"Value={t.split('=', 1)[1] if '=' in t else ''},PropagateAtLaunch=false"
        )
    if not tags:
        return
    aws(["autoscaling", "create-or-update-tags",
         "--region", region, "--tags", *tags])
