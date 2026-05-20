from src.image_family import classify, parse_pull_duration, DEFAULT_FAMILY


def test_classify_cilium_variants():
    assert classify("quay.io/cilium/cilium:v1.16.4") == "cilium"
    assert classify("mcr.microsoft.com/oss/cilium/cilium:1.16.4") == "cilium"
    assert classify("quay.io/cilium/cilium-envoy:v1.31.0") == "cilium"
    assert classify("quay.io/cilium/clustermesh-apiserver:v1.16.4") == "cilium"
    assert classify("quay.io/cilium/operator-generic:v1.16.4") == "cilium"


def test_classify_aks_components():
    assert classify("mcr.microsoft.com/containernetworking/azure-cns:v1.5.32") == "cns"
    assert classify("mcr.microsoft.com/containernetworking/azure-cni:v1.5.32") == "azure-cni"
    assert classify("mcr.microsoft.com/oss/kubernetes/azure-ip-masq-agent:v2.10") == "azure-cni"


def test_classify_csi_azure():
    assert classify("mcr.microsoft.com/oss/kubernetes-csi/azuredisk-csi:v1.30.3") == "csi-azure"
    assert classify("mcr.microsoft.com/oss/kubernetes-csi/azurefile-csi:v1.30.3") == "csi-azure"
    assert classify("mcr.microsoft.com/oss/kubernetes-csi/csi-node-driver-registrar:v2.12.0") == "csi-azure"
    assert classify("mcr.microsoft.com/oss/kubernetes-csi/livenessprobe:v2.13.0") == "csi-azure"


def test_classify_misc():
    assert classify("registry.k8s.io/coredns/coredns:v1.11.3") == "dns"
    assert classify("registry.k8s.io/node-local-dns:1.23.0") == "dns"
    assert classify("registry.k8s.io/kas-network-proxy/proxy-agent:v0.30.3") == "konnectivity"
    assert classify("registry.k8s.io/kube-proxy:v1.30.5") == "kube-proxy"
    assert classify("registry.k8s.io/metrics-server/metrics-server:v0.7.1") == "metrics"


def test_classify_unknown_falls_back_to_other():
    assert classify("docker.io/library/nginx:1.27") == DEFAULT_FAMILY
    assert classify("") == DEFAULT_FAMILY
    assert classify(None) == DEFAULT_FAMILY  # type: ignore[arg-type]


def test_classify_extra_trigger_pattern_wins():
    # Even though pause normally maps to "pause", a caller-supplied
    # trigger pattern matching the same string should take precedence.
    assert classify("registry.k8s.io/pause:3.10",
                    extra_trigger_pattern="pause") == "trigger"
    # If the extra pattern doesn't match, the normal classification applies.
    assert classify("registry.k8s.io/pause:3.10",
                    extra_trigger_pattern="never-matches-x") == "pause"


def test_classify_extra_trigger_pattern_invalid_regex_is_ignored():
    # A bad regex shouldn't crash classify(); it should just be ignored.
    assert classify("quay.io/cilium/cilium:v1.16.4",
                    extra_trigger_pattern="[") == "cilium"


def test_parse_pull_duration_modern_format_prefers_total():
    msg = ('Successfully pulled image "quay.io/cilium/cilium:v1.16.4" '
           'in 5.234s (5.500s including waiting). Image size: 192334432 bytes.')
    assert parse_pull_duration(msg) == 5.5


def test_parse_pull_duration_legacy_format_no_parenthetical():
    msg = 'Successfully pulled image "X" in 3.21s'
    assert parse_pull_duration(msg) == 3.21


def test_parse_pull_duration_milliseconds():
    msg = 'Successfully pulled image "X" in 250ms (300ms including waiting).'
    assert parse_pull_duration(msg) == 0.3


def test_parse_pull_duration_microseconds():
    msg = 'Successfully pulled image "X" in 4500µs'
    assert parse_pull_duration(msg) == 4500 / 1e6


def test_parse_pull_duration_unparseable_returns_none():
    assert parse_pull_duration("") is None
    assert parse_pull_duration("no duration here") is None
    assert parse_pull_duration("Successfully pulled image in abc") is None
