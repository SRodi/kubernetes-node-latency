"""Classify container image references into 'families' for cross-provider
image-pull analysis.

The harness captures every `Pulling`/`Pulled` Event on a fresh node and
classifies each image into a small fixed taxonomy (cilium / cns /
azure-cni / csi / dns / konnectivity / kube-proxy / metrics /
trigger / other). Families are matched in declaration order so the
first match wins; the catch-all `other` ensures every image gets a
family.

Pure stdlib; no kubernetes imports — safe to import from collectors,
records, plotting alike.
"""
from __future__ import annotations

import re

# Order matters: first match wins. Patterns are case-insensitive regexes
# matched against the full image reference (registry/repo:tag).
FAMILY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Cilium core agent + ancillary cilium images. Match both upstream
    # (quay.io/cilium/...) and AKS-managed (mcr.microsoft.com/oss/cilium/...)
    # and the cilium-envoy / clustermesh / hubble / operator-generic sidecars.
    ("cilium", re.compile(
        r"(?:^|/)cilium(?:[-/]|$)|hubble-(?:relay|ui)|"
        r"(?:^|/)operator-generic(?:[:@]|$)",
        re.I)),
    # Azure CNS (Container Networking Service — pod IPAM daemon).
    ("cns", re.compile(r"azure-cns|aks/cns", re.I)),
    # Azure CNI plugin (different from CNS) and ip-masq-agent / npm.
    ("azure-cni", re.compile(r"azure-(?:cni|ip-masq|npm)", re.I)),
    # CSI drivers + upstream sidecars. Covers Azure (azuredisk/azurefile/
    # blob), GCP (gcp-compute-persistent-disk-csi-driver, gcs-fuse,
    # filestore, parallelstore), AWS (ebs-csi), and the cloud-agnostic
    # upstream sidecars (csi-node-driver-registrar, csi-attacher, etc.)
    # that all CSI drivers ship together. They tend to land on a fresh
    # node together so a single family keeps the chart legible.
    ("csi", re.compile(
        r"(?:csi-[a-z-]*(?:azuredisk|azurefile|blob)|"
        r"(?:azuredisk|azurefile|blob)-csi)|"
        r"gcp-compute-persistent-disk-csi-driver|"
        r"gcs-fuse-csi-driver|gcp-filestore-csi-driver|parallelstore-csi-driver|"
        r"(?:^|/)ebs-csi(?:[-/:]|$)|aws-ebs-csi-driver|"
        r"csi-(?:attacher|provisioner|resizer|snapshotter|node-driver-registrar)|"
        r"livenessprobe", re.I)),
    # Cluster DNS (CoreDNS + node-local-dns).
    ("dns", re.compile(r"coredns|node-local-dns|k8s-dns", re.I)),
    # Konnectivity tunnel (GKE / AKS control-plane reach-back). Multiple
    # repo names exist across providers and versions.
    ("konnectivity", re.compile(
        r"konnectivity|kas-network-proxy|apiserver-network-proxy|"
        r"(?:^|/)proxy-agent(?:[:@]|$)", re.I)),
    # kube-proxy on providers that still ship it.
    ("kube-proxy", re.compile(r"kube-proxy", re.I)),
    # Observability sidecars routinely scheduled per node.
    ("metrics", re.compile(
        r"metrics-server|prometheus|fluent-?bit|fluentd|node-exporter|"
        r"otel-collector|opentelemetry", re.I)),
    # Pause sandbox image (containerd/cri-o) — usually pre-baked but emit
    # an event on cold rebuild.
    ("pause", re.compile(r"(?:/|^)pause(?:[:@]|$)|/pause-amd64", re.I)),
    # The harness's own trigger pod. Caller can override via the
    # `extra_trigger_pattern` argument to `classify()`.
    ("trigger", re.compile(r"pause-amd64|gcr\.io/google_containers/pause", re.I)),
]

DEFAULT_FAMILY = "other"

# Stable color palette used by phase_profile + compare plots for the per-family
# bars. Keep in sync with the families above; `other` falls back to grey.
FAMILY_COLORS: dict[str, str] = {
    "cilium":       "#34a853",
    "cns":          "#4285f4",
    "azure-cni":    "#fb8c00",
    "csi":          "#a855f7",
    "dns":          "#ea4335",
    "konnectivity": "#06b6d4",
    "kube-proxy":   "#fbbf24",
    "metrics":      "#ec4899",
    "pause":        "#9ca3af",
    "trigger":      "#94a3b8",
    DEFAULT_FAMILY: "#6b7280",
}


def classify(image_ref: str, *, extra_trigger_pattern: str | None = None) -> str:
    """Return the family for an image reference.

    `extra_trigger_pattern` lets the runner pass the configured trigger-pod
    image substring so iteration-specific workload images get tagged as
    `trigger` rather than `other`.
    """
    if not image_ref:
        return DEFAULT_FAMILY
    if extra_trigger_pattern:
        try:
            if re.search(extra_trigger_pattern, image_ref, re.I):
                return "trigger"
        except re.error:
            pass
    for family, pat in FAMILY_PATTERNS:
        if pat.search(image_ref):
            return family
    return DEFAULT_FAMILY


def image_basename(ref: str) -> str:
    """Strip the registry / repo path AND any digest so only the image name
    (+ tag, when present) remains.

    Examples:
      mcr.microsoft.com/oss/v2/containernetworking/azure-cns:v1.7.16-0
        -> azure-cns:v1.7.16-0
      quay.io/cilium/cilium-distroless:v1.18.9
        -> cilium-distroless:v1.18.9
      registry.k8s.io/pause:3.9
        -> pause:3.9
      gke.gcr.io/anetd@sha256:abc...
        -> anetd
      foo/bar:v1@sha256:abc...
        -> bar:v1
    Empty / None input returns "".
    """
    if not ref:
        return ""
    tail = ref.rsplit("/", 1)[-1]
    # Drop any digest (`@sha256:...`); keep the preceding tag if there is one.
    if "@" in tail:
        tail = tail.split("@", 1)[0]
    return tail


# Families kept as individual lanes on the merged main chart (critical
# path / per-image signal matters). Everything else is collapsed into
# one aggregated lane per family to keep the chart legible on providers
# that pull many ancillary images (notably GKE).
MAIN_CHART_PER_IMAGE_FAMILIES: frozenset[str] = frozenset({
    "cilium", "cns", "azure-cni", "trigger",
})

# Pulls shorter than this are excluded from the main chart (sub-panel and
# JSON still show them). Sub-second pulls are typically cache hits or
# tiny sidecar images that add visual noise without changing the story.
MAIN_CHART_PULL_MIN_S: float = 1.0



_DURATION_RE = re.compile(
    r"in\s+(?P<core>[0-9]+(?:\.[0-9]+)?(?:m?s|µs))"
    r"(?:\s+\((?P<total>[0-9]+(?:\.[0-9]+)?(?:m?s|µs))\s+including waiting\))?",
)


def parse_pull_duration(message: str) -> float | None:
    """Extract pull duration in seconds from a kubelet `Pulled` event message.

    Kubelet 1.24+ emits messages like:
        Successfully pulled image "X" in 5.234s (5.500s including waiting). Image size: ...
    Older versions omit the parenthetical. Returns the *total* (including
    waiting) when present, since that's the wall-clock cost the node paid;
    falls back to `core`. Returns None when neither can be parsed.
    """
    if not message:
        return None
    m = _DURATION_RE.search(message)
    if not m:
        return None
    raw = m.group("total") or m.group("core")
    if not raw:
        return None
    return _parse_duration_token(raw)


def _parse_duration_token(tok: str) -> float | None:
    """Parse a duration token of the form '5.234s', '120ms', '4500µs'."""
    if tok.endswith("µs"):
        try:
            return float(tok[:-2]) / 1e6
        except ValueError:
            return None
    if tok.endswith("ms"):
        try:
            return float(tok[:-2]) / 1e3
        except ValueError:
            return None
    if tok.endswith("s"):
        try:
            return float(tok[:-1])
        except ValueError:
            return None
    return None
