"""Unit tests for log-derived sub-phase extraction (src/log_phases.py).

Fixture log text below is trimmed from real `--capture-logs minimal`
captures (results/instrumentation-probe, cilium v1.19.8 / k8s 1.36.4)
used to root-cause the `run:cni-installer`, `run:cilium-init-all`, and
"Agent main container startup" delays.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src import log_phases
from src.records import IterationRecord

CILIUM_INIT_ALL_LOG = """\
2026-10-08T11:01:35.780000000Z Installing cilium-cni to /host/opt/cni/bin/cilium-cni ...
2026-10-08T11:01:46.060000000Z Wrote /host/opt/cni/bin/cilium-cni
2026-10-08T11:01:46.100000000Z Mounting cgroup filesystem
2026-10-08T11:01:46.300000000Z Applying sysctl overwrites
2026-10-08T11:01:46.500000000Z Mounting bpf filesystem
2026-10-08T11:01:46.700000000Z Installing iptables rule (1/2)
2026-10-08T11:01:46.900000000Z Installing iptables rule (2/2)
2026-10-08T11:01:47.050000000Z cilium-init-all complete
"""

CNI_INSTALLER_LOG = """\
2026-10-08T11:01:34.100000000Z ts=1791402094.1 level=info msg="wrote file" sources=azure-ipam outputs=/opt/cni/bin/azure-ipam cmd=deploy src=azure-ipam dest=/opt/cni/bin/azure-ipam
2026-10-08T11:01:34.120000000Z ts=1791402094.12 level=info msg="successfully wrote files" sources=azure-ipam outputs=/opt/cni/bin/azure-ipam
"""

CILIUM_AGENT_LOG = """\
2026-10-08T11:02:06.253000000Z time="2026-10-08T11:02:06Z" level=info msg="option parsing complete"
2026-10-08T11:02:06.882000000Z time="2026-10-08T11:02:06Z" level=info msg="Start hook executed" duration=151.801823ms function=local-node-store
2026-10-08T11:02:07.046000000Z time="2026-10-08T11:02:07Z" level=info msg="Initializing daemon"
2026-10-08T11:02:07.074000000Z time="2026-10-08T11:02:07Z" level=info msg="Creating or updating CiliumNode resource"
2026-10-08T11:02:07.113000000Z time="2026-10-08T11:02:07Z" level=info msg="Initializing identity allocator"
2026-10-08T11:02:07.114000000Z time="2026-10-08T11:02:07Z" level=info msg="Serving cilium API at unix:///var/run/cilium/cilium.sock"
2026-10-08T11:02:09.181000000Z time="2026-10-08T11:02:09Z" level=info msg="Compiled new BPF template" BPFCompilationTime=1.220896469s
2026-10-08T11:02:09.250000000Z time="2026-10-08T11:02:09Z" level=info msg="Program attached to device" device=eth0
2026-10-08T11:02:09.500000000Z time="2026-10-08T11:02:09Z" level=info msg="Program attached to device" device=cilium_host
2026-10-08T11:02:09.600000000Z time="2026-10-08T11:02:09Z" level=info msg="Serving cilium health API at unix:///var/run/cilium/health.sock"
"""


def test_parse_cilium_init_all_isolates_cni_binary_write():
    out = log_phases.parse_cilium_init_all(CILIUM_INIT_ALL_LOG)
    assert out["cni_binary_write_s"] == 10.28
    assert out["first_ts"] == datetime(2026, 10, 8, 11, 1, 35, 780000, tzinfo=timezone.utc)
    assert out["last_ts"] == datetime(2026, 10, 8, 11, 1, 47, 50000, tzinfo=timezone.utc)


def test_parse_cni_installer_span():
    out = log_phases.parse_cni_installer(CNI_INSTALLER_LOG)
    assert round((out["last_ts"] - out["first_ts"]).total_seconds(), 3) == 0.02


def test_parse_cilium_agent_milestones():
    out = log_phases.parse_cilium_agent(CILIUM_AGENT_LOG)
    assert out["initializing_daemon"] == datetime(2026, 10, 8, 11, 2, 7, 46000, tzinfo=timezone.utc)
    assert out["serving_cilium_api"] == datetime(2026, 10, 8, 11, 2, 7, 114000, tzinfo=timezone.utc)
    assert out["bpf_compiled_first"] == datetime(2026, 10, 8, 11, 2, 9, 181000, tzinfo=timezone.utc)
    # "_last" milestone keeps the *last* matching line, not the first.
    assert out["program_attached_last"] == datetime(2026, 10, 8, 11, 2, 9, 500000, tzinfo=timezone.utc)
    assert out["serving_health_api"] == datetime(2026, 10, 8, 11, 2, 9, 600000, tzinfo=timezone.utc)


def test_parse_empty_log_returns_empty_dict():
    assert log_phases.parse_cilium_agent("") == {}
    assert log_phases.parse_cilium_init_all("not a timestamped line\n") == {}


def test_compute_phase_breakdown_end_to_end(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "kube-system__cilium-jmhjb__cilium-init-all.log").write_text(CILIUM_INIT_ALL_LOG)
    (log_dir / "kube-system__azure-cns-59hhs__cni-installer.log").write_text(CNI_INSTALLER_LOG)
    (log_dir / "kube-system__cilium-jmhjb__cilium-agent.log").write_text(CILIUM_AGENT_LOG)

    rec = IterationRecord(iteration=1, run_id="test", provider="existing", region="westus2")
    rec.T3_cilium_ready = datetime(2026, 10, 8, 11, 2, 8, tzinfo=timezone.utc)
    rec.init_containers = [
        {"name": "cilium-init-all",
         "started_at": datetime(2026, 10, 8, 11, 1, 35, tzinfo=timezone.utc),
         "finished_at": datetime(2026, 10, 8, 11, 1, 50, tzinfo=timezone.utc)},
    ]
    rec.node_container_starts = [
        {"namespace": "kube-system", "pod": "azure-cns-59hhs", "container": "cni-installer",
         "init": True, "t_started": "2026-10-08T11:01:34+00:00"},
        {"namespace": "kube-system", "pod": "azure-cns-59hhs", "container": "cns-container",
         "init": False, "t_started": "2026-10-08T11:01:46+00:00"},
        {"namespace": "kube-system", "pod": "cilium-jmhjb", "container": "cilium-agent",
         "init": False, "t_started": "2026-10-08T11:02:06+00:00"},
    ]
    rec.node_image_pulls = [
        {"pod": "cilium-jmhjb", "namespace": "kube-system", "container": "cilium-agent",
         "image": "cilium-distroless:v1.19.8", "family": "cilium",
         "t_pulling": None, "t_pulled": "2026-10-08T11:01:48+00:00",
         "duration_s": None, "failed": False},
    ]

    out = log_phases.compute_phase_breakdown(rec, log_dir)

    # cilium-init-all: ~2.95s of kubelet-reported finish is unexplained
    # after the container's own last log line.
    assert out["cilium_init_all_cni_binary_write_s"] == 10.28
    assert round(out["cilium_init_all_post_log_gap_s"], 2) == 2.95

    # cni-installer: next container (cns-container) started ~11.88s
    # after this container's last log line — the "dead gap".
    assert round(out["cni_installer_post_log_gap_s"], 2) == 11.88

    # Agent: pull-to-start gap (18s) dwarfs internal bootstrap (~2s),
    # proving the delay is a pre-start scheduling gap, not agent-internal
    # work.
    assert out["agent_pull_to_start_gap_s"] == 18.0
    assert out["agent_internal_bootstrap_s"] == 2.0
    assert out["agent_log_initializing_daemon_offset_s"] == 1.046
    assert out["agent_log_serving_cilium_api_offset_s"] == 1.114
    assert round(out["agent_log_bpf_compiled_first_offset_s"], 3) == 3.181
    assert round(out["agent_log_program_attached_last_offset_s"], 1) == 3.5


def test_compute_phase_breakdown_missing_data_is_best_effort(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    rec = IterationRecord(iteration=1, run_id="test", provider="existing", region="westus2")
    out = log_phases.compute_phase_breakdown(rec, log_dir)
    assert out == {}


def test_record_to_row_merges_log_phase_breakdown():
    rec = IterationRecord(iteration=1, run_id="test", provider="existing", region="westus2")
    rec.log_phase_breakdown = {"agent_pull_to_start_gap_s": 18.0}
    row = rec.to_row()
    assert row["agent_pull_to_start_gap_s"] == 18.0
