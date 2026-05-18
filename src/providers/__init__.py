"""Provider registry."""
from __future__ import annotations

from .base import ClusterHandle, ClusterProvider
from .gke_autopilot import GKEAutopilotProvider
from .gke_standard_dpv2 import GKEStandardDPv2Provider
from .existing import ExistingProvider
from .aks_overlay_cilium import AKSOverlayCiliumProvider
from .aks_byocni import AKSBYOCNIProvider
from .aks_kubenet import AKSKubenetProvider
from .eks_eni_cilium import EKSEniCiliumProvider

_PROVIDERS = {
    GKEAutopilotProvider.name: GKEAutopilotProvider,
    GKEStandardDPv2Provider.name: GKEStandardDPv2Provider,
    ExistingProvider.name: ExistingProvider,
    AKSOverlayCiliumProvider.name: AKSOverlayCiliumProvider,
    AKSBYOCNIProvider.name: AKSBYOCNIProvider,
    AKSKubenetProvider.name: AKSKubenetProvider,
    EKSEniCiliumProvider.name: EKSEniCiliumProvider,
}


def get(name: str, cfg) -> ClusterProvider:
    if name not in _PROVIDERS:
        raise KeyError(f"unknown provider '{name}'. available: {sorted(_PROVIDERS)}")
    return _PROVIDERS[name](cfg)


__all__ = ["ClusterHandle", "ClusterProvider", "get"]
