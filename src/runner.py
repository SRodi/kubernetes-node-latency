"""Iteration loop: submit trigger pod, capture T0-T4, cleanup."""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Iterable

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from kubernetes import client, config as kubeconfig

from .collectors import (Collector, EventSink, _parse_k8s_time, cordon_nodes,
                         list_node_names, uncordon_nodes)
from .config import Config
from .providers.base import ClusterHandle, ClusterProvider
from .records import IterationRecord, utcnow

log = logging.getLogger(__name__)

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "manifests"


def load_kube(kubeconfig_path: Path) -> client.CoreV1Api:
    os.environ["KUBECONFIG"] = str(kubeconfig_path)
    kubeconfig.load_kube_config(config_file=str(kubeconfig_path))
    return client.CoreV1Api()


def wait_for_apiserver(core: client.CoreV1Api, *, timeout_s: int = 600,
                        poll_interval_s: float = 10.0) -> bool:
    """Block until the apiserver answers a cheap call, or timeout.

    Newly-created AKS clusters report ``provisioningState=Succeeded`` to the
    Azure control plane several minutes before the HCP-fronted apiserver
    hostname is actually reachable from the harness host. Without this probe
    the very first ``list_node`` call would burn the urllib3 default retry
    budget (3 × ~2-minute connect timeouts = ~6 minutes) before raising.

    Uses ``_request_timeout=(connect=5, read=10)`` so we fail fast and retry
    on a fresh interval. Returns True once the apiserver is reachable, False
    if the timeout elapses.
    """
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            core.list_node(limit=1, _request_timeout=(5, 10))
            log.info("apiserver reachable after %d attempt(s)", attempt)
            return True
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 1 or attempt % 6 == 0:
                log.info("waiting for apiserver (attempt %d): %s",
                         attempt, type(e).__name__)
            time.sleep(poll_interval_s)
    log.warning("apiserver unreachable after %ss: %s", timeout_s, last_err)
    return False


def render_pod(cfg: Config, *, run_id: str, iteration: int, provider: ClusterProvider) -> dict:
    env = Environment(loader=FileSystemLoader(str(MANIFEST_DIR)),
                      autoescape=select_autoescape(disabled_extensions=("j2",)))
    tmpl = env.get_template("trigger-pod.yaml.j2")
    hint = provider.node_autoprovision_hint()
    text = tmpl.render(
        pod_name=f"latency-trigger-{iteration:03d}-{uuid.uuid4().hex[:6]}",
        namespace=cfg.trigger_pod.namespace,
        iteration=iteration,
        run_id=run_id,
        image=cfg.trigger_pod.image,
        cpu=cfg.trigger_pod.cpu,
        memory=cfg.trigger_pod.memory,
        node_selector=hint.get("nodeSelector") or {},
        tolerations=hint.get("tolerations") or [],
    )
    return yaml.safe_load(text)


def submit_pod(core: client.CoreV1Api, manifest: dict) -> client.V1Pod:
    return core.create_namespaced_pod(namespace=manifest["metadata"]["namespace"],
                                       body=manifest)


def delete_pod(core: client.CoreV1Api, name: str, namespace: str) -> None:
    try:
        core.delete_namespaced_pod(name=name, namespace=namespace,
                                   grace_period_seconds=0, propagation_policy="Foreground")
    except client.ApiException as e:
        if getattr(e, "status", None) == 404:
            # Already gone (e.g. AKS NAP reaped the node + pod). Not an error.
            return
        log.warning("delete pod %s failed: %s", name, e)


def run_iterations(cfg: Config, handle: ClusterHandle, provider: ClusterProvider,
                   run_dir: Path, run_id: str) -> list[IterationRecord]:
    core = load_kube(handle.kubeconfig)
    apps = client.AppsV1Api()
    probe = provider.cni_probe()

    records: list[IterationRecord] = []
    with EventSink(run_dir / "raw_events.jsonl") as sink:
        # One-shot snapshot of Cilium agent + operator configuration.
        # Best-effort: it must never break the run.
        try:
            from . import cilium_config as _cc
            _cc.snapshot(core, apps,
                         namespace=probe.namespace,
                         agent_label_selector=probe.label_selector,
                         out_dir=run_dir / "cilium_config")
        except Exception as e:  # noqa: BLE001
            log.warning("cilium config snapshot failed: %s", e)

        for i in range(1, cfg.iterations + 1):
            rec = IterationRecord(iteration=i, run_id=run_id,
                                  provider=provider.name, region=handle.region)
            log.info("=== iteration %d/%d ===", i, cfg.iterations)
            cordoned: list[str] = []
            try:
                getattr(provider, "pre_iteration", lambda *a, **kw: None)(handle, i)
                before = list_node_names(core)
                sink.write("iteration_start", {"iteration": i, "pre_existing_nodes": sorted(before)})

                cordoned = cordon_nodes(core, before, sink)

                manifest = render_pod(cfg, run_id=run_id, iteration=i, provider=provider)
                rec.pod_name = manifest["metadata"]["name"]
                pod = submit_pod(core, manifest)
                rec.T0_pod_created = _parse_k8s_time(pod.metadata.creation_timestamp) or utcnow()
                sink.write("pod_created", {"pod": rec.pod_name,
                                            "ts": rec.T0_pod_created.isoformat()})

                collector = Collector(core, probe, sink)
                rec.node_name, rec.T1_node_registered = collector.wait_for_new_node(
                    before, timeout_s=cfg.per_iteration_timeout_s,
                    not_before=rec.T0_pod_created)
                rec.T4_node_ready, rec.T4b_schedulable, rec.T1c_cni_conflist, \
                    rec.T_csinode_ready, rec.T_taint_observed = (
                        collector.wait_for_node_ready(
                            rec.node_name, timeout_s=cfg.per_iteration_timeout_s))

                agent = None if probe.skip else collector.find_agent_pod(rec.node_name, timeout_s=120)
                if probe.skip:
                    sink.write("cni_probe_skipped",
                               {"node": rec.node_name, "probe": probe.name,
                                "reason": "no per-node CNI agent (e.g. kubenet)"})
                elif agent is not None:
                    # Primary path: pod-watch (no log access required).
                    t2, t3 = collector.watch_agent_pod(
                        agent.metadata.name,
                        timeout_s=cfg.per_iteration_timeout_s,
                    )
                    rec.T2_cilium_started = t2
                    rec.T3_cilium_ready = t3
                    # Fallback: log-scan if pod condition didn't yield T3.
                    if rec.T3_cilium_ready is None:
                        rec.T3_cilium_ready = collector.t3_ready_from_logs(
                            agent, tail_lines=cfg.cni.log_tail_lines)
                    # T1\u2192T1c enrichment: re-read pod for init container
                    # statuses and grab pod-scoped events for image-pull and
                    # PodScheduled timestamps. Best-effort.
                    try:
                        lc = collector.collect_pod_lifecycle(
                            agent.metadata.name, probe.namespace)
                        rec.T_pod_scheduled = lc.get("T_pod_scheduled")
                        rec.T_pod_initialized = lc.get("T_pod_initialized")
                        rec.T_image_pull_start = lc.get("T_image_pull_start")
                        rec.T_image_pulled = lc.get("T_image_pulled")
                        rec.init_containers = lc.get("init_containers") or None
                        rec.pod_events = lc.get("events") or None
                    except Exception as e:  # noqa: BLE001
                        log.warning("pod-lifecycle enrichment failed for iter %d: %s", i, e)
                else:
                    sink.write("cni_probe_unavailable",
                               {"node": rec.node_name, "probe": probe.name})

                if cfg.cni.deep and agent is not None and rec.T3_cilium_ready is not None:
                    from .cilium_deep import collect as collect_deep
                    iter_dir = run_dir / f"iter-{i:03d}"
                    rec.deep_cilium = collect_deep(
                        core, agent_pod=agent, probe=probe,
                        node_name=rec.node_name,
                        iter_dir=iter_dir,
                        metrics_ports=cfg.cni.metrics_ports,
                        scraper_image=cfg.cni.deep_scraper_image,
                        scraper_namespace=cfg.cni.deep_scraper_namespace,
                    ) or None
                    sink.write("cilium_deep_collected",
                               {"iteration": i,
                                "have_metrics": "metrics" in (rec.deep_cilium or {})})

                # T5: explicitly wait for the trigger pod's first container
                # to reach Running (== sandbox wired, CNI ADD done). This is
                # the numerator of the headline `time_to_runnable_s` KPI and
                # must be waited on deterministically — the trigger pod goes
                # Running a few seconds AFTER the CNI agent is Ready (T3), so
                # without this the finally-block read races the container
                # start and yields a null T5 on fast providers (only masked
                # previously by the incidental wall-clock delay of
                # --deep-cilium metric scraping).
                rec.T5_pod_running = collector.wait_for_pod_running(
                    rec.pod_name, cfg.trigger_pod.namespace,
                    timeout_s=cfg.per_iteration_timeout_s,
                )

                rec.status = "success"
            except TimeoutError as e:
                rec.status = "timeout"
                rec.error = str(e)
                log.error("iteration %d timed out: %s", i, e)
            except Exception as e:
                rec.status = "error"
                rec.error = repr(e)
                log.exception("iteration %d errored", i)
            finally:
                # Trigger-pod lifecycle capture — read pod state BEFORE
                # delete_pod so we can record T_trigger_scheduled and (as a
                # backstop) T5_pod_running. The primary T5 signal comes from
                # the explicit wait_for_pod_running above; here we only fill
                # gaps and never overwrite an already-captured T5 with a
                # null re-read. Best-effort; never raises. Guarded by
                # locals().get() because `collector` may not be defined
                # yet if we errored before instantiation.
                _coll = locals().get("collector")
                if rec.pod_name and rec.T0_pod_created is not None and _coll is not None:
                    try:
                        tp = _coll.collect_trigger_pod_status(
                            rec.pod_name, cfg.trigger_pod.namespace)
                        rec.T_trigger_scheduled = tp.get("T_trigger_scheduled")
                        if rec.T5_pod_running is None:
                            rec.T5_pod_running = tp.get("T5_pod_running")
                    except Exception as e:  # noqa: BLE001
                        log.warning("trigger-pod status capture failed for iter %d: %s", i, e)
                # Image-pull capture on the new node. Bounded by
                # [T1, T5+5s] when both anchors are present, else fall back
                # to [T0, now] so we still capture something on partial
                # failures. Best-effort; never raises.
                if rec.node_name and _coll is not None:
                    try:
                        win_start = rec.T1_node_registered or rec.T0_pod_created
                        win_end = (
                            (rec.T5_pod_running + timedelta(seconds=5))
                            if rec.T5_pod_running is not None
                            else utcnow()
                        )
                        if win_start is not None and win_end > win_start:
                            rec.node_image_pulls = _coll.collect_node_image_pulls(
                                rec.node_name, win_start, win_end,
                                trigger_pattern=cfg.trigger_pod.image,
                            ) or None
                            rec.node_container_starts = _coll.collect_node_container_starts(
                                rec.node_name, win_start, win_end,
                            ) or None
                            rec.node_container_creates = _coll.collect_node_container_creates(
                                rec.node_name, win_start, win_end,
                            ) or None
                            rec.node_pod_events = _coll.collect_node_pod_events(
                                rec.node_name, win_start, win_end,
                            ) or None
                            rec.node_pod_status = _coll.collect_node_pod_status(
                                rec.node_name,
                            ) or None
                    except Exception as e:  # noqa: BLE001
                        log.warning("node image-pull capture failed for iter %d: %s", i, e)
                # Cluster-manifest snapshot — one-shot per run, fired
                # on the FIRST iteration once we have a fresh node so the
                # kubelet configz endpoint is reachable. Captures kubelet
                # effective config (the authoritative source of
                # serializeImagePulls / maxParallelImagePulls), all
                # DaemonSets in kube-system, and key ConfigMaps. Best
                # effort; never breaks the run.
                if (i == 1 and rec.node_name
                        and not getattr(run_iterations, "_snapshot_done", False)):
                    try:
                        from . import cluster_snapshot as _cs
                        apps_api = client.AppsV1Api(core.api_client)
                        _cs.snapshot(core, apps_api,
                                     node=rec.node_name,
                                     out_dir=run_dir / "cluster_manifests")
                        setattr(run_iterations, "_snapshot_done", True)
                    except Exception as e:  # noqa: BLE001
                        log.warning("cluster manifest snapshot failed: %s", e)
                # Pod-log capture (opt-in via --capture-logs). Bounded to the
                # iteration window so we don't drag in unrelated history.
                if (getattr(cfg, "capture_logs", "none") == "minimal"
                        and rec.node_name and _coll is not None):
                    try:
                        iter_dir = run_dir / f"iter-{i:03d}"
                        log_dest = iter_dir / "logs"
                        # Cilium agents + CNS/IPAM (best-effort across providers).
                        targets = [
                            ("kube-system", "k8s-app=cilium"),
                            ("kube-system", "k8s-app=aws-node"),
                            ("kube-system", "k8s-app=azure-cns"),
                            ("kube-system", "k8s-app=cilium-azure-ipam"),
                        ]
                        since = rec.T1_node_registered or rec.T0_pod_created
                        t0_log = time.monotonic()
                        summary = _coll.collect_pod_logs(
                            rec.node_name, log_dest,
                            targets=targets, since_time=since,
                        )
                        summary["duration_s"] = round(time.monotonic() - t0_log, 3)
                        rec.log_capture = summary
                    except Exception as e:  # noqa: BLE001
                        log.warning("pod log capture failed for iter %d: %s", i, e)
                if rec.pod_name:
                    delete_pod(core, rec.pod_name, cfg.trigger_pod.namespace)
                # Best-effort uncordon so leftover nodes can be reused/reaped normally.
                if cordoned:
                    uncordon_nodes(core, cordoned, sink)
                getattr(provider, "post_iteration", lambda *a, **kw: None)(handle, i)
                records.append(rec)
                sink.write("iteration_end", {"iteration": i, **rec.to_row()})
                if i < cfg.iterations:
                    time.sleep(cfg.node_settle_seconds)
    return records
