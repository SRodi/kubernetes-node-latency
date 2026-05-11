"""Provider registry."""
from __future__ import annotations

from .base import ClusterHandle, ClusterProvider
from .gke_autopilot import GKEAutopilotProvider
from .gke_standard_dpv2 import GKEStandardDPv2Provider
from .existing import ExistingProvider
from .aks_overlay_cilium import AKSOverlayCiliumProvider
from .aks_byocni import AKSBYOCNIProvider

_PROVIDERS = {
    GKEAutopilotProvider.name: GKEAutopilotProvider,
    GKEStandardDPv2Provider.name: GKEStandardDPv2Provider,
    ExistingProvider.name: ExistingProvider,
    AKSOverlayCiliumProvider.name: AKSOverlayCiliumProvider,
    AKSBYOCNIProvider.name: AKSBYOCNIProvider,
}


def get(name: str, cfg) -> ClusterProvider:
    if name not in _PROVIDERS:
        raise KeyError(f"unknown provider '{name}'. available: {sorted(_PROVIDERS)}")
    return _PROVIDERS[name](cfg)


__all__ = ["ClusterHandle", "ClusterProvider", "get"]
