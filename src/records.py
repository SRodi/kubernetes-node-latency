"""Data records persisted across runs."""
from __future__ import annotations

import dataclasses as dc
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).isoformat() if ts else None


def delta(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


@dc.dataclass
class IterationRecord:
    iteration: int
    run_id: str
    provider: str
    region: str

    pod_name: str | None = None
    node_name: str | None = None

    T0_pod_created: datetime | None = None
    T1_node_registered: datetime | None = None
    T2_cilium_started: datetime | None = None
    T3_cilium_ready: datetime | None = None
    T4_node_ready: datetime | None = None

    status: str = "pending"  # pending|success|timeout|error
    error: str | None = None

    def to_row(self) -> dict:
        return {
            "iteration": self.iteration,
            "run_id": self.run_id,
            "provider": self.provider,
            "region": self.region,
            "node_name": self.node_name,
            "pod_name": self.pod_name,
            "T0_pod_created": iso(self.T0_pod_created),
            "T1_node_registered": iso(self.T1_node_registered),
            "T2_cilium_started": iso(self.T2_cilium_started),
            "T3_cilium_ready": iso(self.T3_cilium_ready),
            "T4_node_ready": iso(self.T4_node_ready),
            "node_startup_latency_s": delta(self.T4_node_ready, self.T0_pod_created),
            "node_register_latency_s": delta(self.T1_node_registered, self.T0_pod_created),
            "cilium_init_duration_s": delta(self.T3_cilium_ready, self.T2_cilium_started),
            "cni_induced_delay_s": (
                max(delta(self.T4_node_ready, self.T3_cilium_ready) or 0.0, 0.0)
                if (self.T3_cilium_ready and self.T4_node_ready) else None
            ),
            "status": self.status,
            "error": self.error,
        }
