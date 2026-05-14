"""AKS with the legacy kubenet network plugin.

Kubenet uses kubelet's built-in bridge plugin + host-local IPAM and
relies on kube-proxy (iptables) for service routing. There is no per-node
CNI agent DaemonSet, so this provider attaches the `noop` CNI probe:
the harness records T0/T1/T4 normally and leaves T2/T3 (and therefore
`cilium_init_duration_s` / `cni_induced_delay_s`) as null. Only the
primary KPI `node_startup_latency_s = T4 − T0` and `node_register_latency_s
= T1 − T0` are comparable to the Cilium-based providers.

Caveat: kubenet is deprecated in AKS and is unavailable on newer
Kubernetes versions. Pin `aks.kubernetes_version` to a supported
release (e.g. 1.28) when using this provider, otherwise `az aks create`
will reject the `--network-plugin kubenet` flag.
"""
from __future__ import annotations

from ..cni import get as get_probe
from ..cni.base import CNIProbe
from ._aks_base import AKSProviderBase


class AKSKubenetProvider(AKSProviderBase):
    name = "aks_kubenet"

    def _az_create_cluster_args(self, cfg) -> list[str]:
        return ["--network-plugin", "kubenet"]

    def _network_describe(self) -> dict:
        return {
            "network_plugin": "kubenet",
            "network_dataplane": "iptables",
            "cni_agent": None,
        }

    def cni_probe(self) -> CNIProbe:
        # Honour explicit override (e.g. for experiments) but default to noop
        # since kubenet has no agent DaemonSet to probe.
        return get_probe(self.cfg.cni.probe or "noop")
