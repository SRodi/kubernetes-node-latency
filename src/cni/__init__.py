"""CNI probe registry."""
from __future__ import annotations

from .base import CNIProbe
from . import aws_vpc, cilium_dpv2, cilium_generic, noop

REGISTRY: dict[str, CNIProbe] = {
    cilium_dpv2.PROBE.name: cilium_dpv2.PROBE,
    cilium_generic.PROBE.name: cilium_generic.PROBE,
    aws_vpc.PROBE.name: aws_vpc.PROBE,
    noop.PROBE.name: noop.PROBE,
}


def get(name: str) -> CNIProbe:
    if name not in REGISTRY:
        raise KeyError(f"unknown CNI probe '{name}'. available: {sorted(REGISTRY)}")
    return REGISTRY[name]


__all__ = ["CNIProbe", "REGISTRY", "get"]
