"""Iteration loop: submit trigger pod, capture T0-T4, cleanup."""
from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Iterable

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from kubernetes import client, config as kubeconfig

from .collectors import Collector, EventSink, _parse_k8s_time, list_node_names
from .config import Config
from .providers.base import ClusterHandle, ClusterProvider
from .records import IterationRecord, utcnow

log = logging.getLogger(__name__)

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "manifests"


def load_kube(kubeconfig_path: Path) -> client.CoreV1Api:
    os.environ["KUBECONFIG"] = str(kubeconfig_path)
    kubeconfig.load_kube_config(config_file=str(kubeconfig_path))
    return client.CoreV1Api()


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
        log.warning("delete pod %s failed: %s", name, e)


def run_iterations(cfg: Config, handle: ClusterHandle, provider: ClusterProvider,
                   run_dir: Path, run_id: str) -> list[IterationRecord]:
    core = load_kube(handle.kubeconfig)
    probe = provider.cni_probe()
    sink = EventSink(run_dir / "raw_events.jsonl")
    records: list[IterationRecord] = []
    try:
        for i in range(1, cfg.iterations + 1):
            rec = IterationRecord(iteration=i, run_id=run_id,
                                  provider=provider.name, region=handle.region)
            log.info("=== iteration %d/%d ===", i, cfg.iterations)
            try:
                before = list_node_names(core)
                sink.write("iteration_start", {"iteration": i, "pre_existing_nodes": sorted(before)})

                manifest = render_pod(cfg, run_id=run_id, iteration=i, provider=provider)
                rec.pod_name = manifest["metadata"]["name"]
                pod = submit_pod(core, manifest)
                rec.T0_pod_created = _parse_k8s_time(pod.metadata.creation_timestamp) or utcnow()
                sink.write("pod_created", {"pod": rec.pod_name,
                                            "ts": rec.T0_pod_created.isoformat()})

                collector = Collector(core, probe, sink)
                rec.node_name, rec.T1_node_registered = collector.wait_for_new_node(
                    before, timeout_s=cfg.per_iteration_timeout_s)
                rec.T4_node_ready = collector.wait_for_node_ready(
                    rec.node_name, timeout_s=cfg.per_iteration_timeout_s)

                agent = collector.find_agent_pod(rec.node_name, timeout_s=120)
                if agent is not None:
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
                else:
                    sink.write("cni_probe_unavailable",
                               {"node": rec.node_name, "probe": probe.name})

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
                if rec.pod_name:
                    delete_pod(core, rec.pod_name, cfg.trigger_pod.namespace)
                records.append(rec)
                sink.write("iteration_end", {"iteration": i, **rec.to_row()})
                if i < cfg.iterations:
                    time.sleep(cfg.node_settle_seconds)
    finally:
        sink.close()
    return records
