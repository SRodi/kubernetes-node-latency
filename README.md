# node-startup-latency

Measures end-to-end **node startup latency** (`Pod created → Node Ready=True`)
on managed Kubernetes platforms, with a uniform measurement methodology across
GKE Autopilot, GKE Standard with Dataplane V2, AKS with Azure CNI Powered by
Cilium, and AKS BYOCNI + upstream Cilium.

## Methodology

Each iteration submits a single resource-heavy trigger Pod with `podAntiAffinity`
against earlier iterations, forcing the platform to provision a brand-new node
VM, while the harness watches the Kubernetes API to capture five timestamps
from a common cluster clock: T0 Pod created, T1 Node registered, T2 CNI agent
container started, T3 CNI agent Ready, T4 Node `Ready=True`. The primary KPI
is `node_startup_latency = T4 − T0`; CNI's contribution is `T3 − T2` (parallel
with node init) and any CNI-induced delay is `max(0, T3 − T4)`. The same code
path runs across all four providers — only the cluster-creation primitive and
the new-node trigger mechanism (Autopilot / NAP / cluster-autoscaler / manual
scale) differ per platform.

### Timestamps and derived metrics

| Marker | Source |
|---|---|
| **T0** Pod created | `Pod.metadata.creationTimestamp` |
| **T1** Node registered | new `Node` first observed via watch (`creationTimestamp`) |
| **T2** CNI agent container started | `pod.status.containerStatuses[*].state.running.startedAt` |
| **T3** CNI agent Ready | agent Pod's `Ready` condition `lastTransitionTime` |
| **T4** Node `Ready=True` | `Node.status.conditions[Ready].lastTransitionTime` |

Derived metrics (seconds):

- `node_startup_latency_s  = T4 − T0` *(primary KPI)*
- `node_register_latency_s = T1 − T0`
- `cilium_init_duration_s  = T3 − T2`
- `cni_induced_delay_s     = max(T4 − T3, 0)`

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Per-provider prerequisites:

| Providers | Required tools |
|---|---|
| `gke_autopilot`, `gke_standard_dpv2` | `gcloud` authenticated |
| `aks_overlay_cilium` | `az` logged in (`az login`), `kubectl` |
| `aks_byocni` | `az` logged in, `kubectl`, `helm` |
| `existing` | a working `kubeconfig` for the target cluster |

## Run

```bash
.venv/bin/python -m src.cli run \
    --provider gke_autopilot \
    --region europe-west1 \
    --iterations 10
```

One command per supported scenario:

```bash
# GKE Autopilot (managed Cilium / Dataplane V2)
.venv/bin/python -m src.cli run --provider gke_autopilot     --region europe-west1 --iterations 10

# GKE Standard with Dataplane V2 (Cilium)
.venv/bin/python -m src.cli run --provider gke_standard_dpv2 --region europe-west1 --iterations 10

# AKS with Azure CNI Powered by Cilium (managed dataplane)
.venv/bin/python -m src.cli run --provider aks_overlay_cilium --region westeurope --iterations 10

# AKS BYOCNI + upstream Cilium installed via Helm
.venv/bin/python -m src.cli run --provider aks_byocni        --region westeurope --iterations 10

# Re-use a cluster you already created
.venv/bin/python -m src.cli run --provider existing --iterations 5
```

### AKS configuration

Set provider-specific options under the `aks:` block in `config.yaml`:

```yaml
provider: aks_overlay_cilium      # or aks_byocni
region: westeurope
aks:
  resource_group: node-latency-rg
  location: westeurope            # defaults to top-level region
  kubernetes_version: null        # null => AKS default
  node_provisioning: cluster_autoscaler   # cluster_autoscaler | nap | manual
  system_node_pool:
    name: systempool
    vm_size: Standard_D4s_v5
    node_count: 1
  user_node_pool:                 # ignored when node_provisioning=nap
    name: latencypool
    vm_size: Standard_D4s_v5
    min_count: 0
    max_count: 50                 # CA holds nodes ~10 min before scale-down
    node_count: 0                 # baseline; manual mode scales to N+1 each iteration
  byocni:                         # only consumed by aks_byocni
    cilium_chart_version: "1.19.3"
    cilium_repo_url: https://helm.cilium.io/
    cilium_values:
      kubeProxyReplacement: "true"
      operator.replicas: "1"
    install_timeout_s: 600
  keep_resource_group: true
```

`node_provisioning` can also be set per run with `--aks-node-provisioning {cluster_autoscaler|nap|manual}`.

| Mode | Behavior |
|---|---|
| `cluster_autoscaler` (default) | User pool starts at `node_count`; CA scales up to satisfy each trigger Pod. |
| `nap` | AKS Node Auto-Provisioning enabled; no user pool added. Closest analog to Autopilot. |
| `manual` | Harness scales the user pool by +1 before each iteration and back down after. |

> **Cost note**: each run provisions a real cluster. Always let the harness
> `delete` it; pass `--keep-cluster` only for debugging.

### Running multiple tests in parallel

Independent runs from separate terminals are safe. Each run gets its own
`results/<run_id>/` directory, its own kubeconfig at
`results/<run_id>/kubeconfig`, and a unique cluster name suffixed with the
last 6 chars of the `run_id` (e.g. `node-latency-test-152203`). Pass
`--cluster-name` explicitly to opt out of the suffix.

## Outputs

Each run writes to `results/<run_id>/`:

```
results/20260512-085541/
├── run_metadata.json      # run identity, effective config, cluster + CNI facts
├── raw_events.jsonl       # every watcher event for offline replay
├── iterations.csv         # per-iteration row (T0..T4 + derived metrics)
├── summary.csv            # aggregate stats per metric
├── summary.md             # human-readable Markdown report
├── summary.json           # machine-readable summary
├── iter-001/              # only with --deep-cilium: per-iteration Cilium artefacts
│   ├── cilium_status.json        # `cilium status -o json --verbose` (bootstrap timings, IPAM, KPR…)
│   ├── cilium_metrics.txt        # raw Prometheus dump
│   └── cilium_deep_headline.json # parsed headline numbers (also merged into iterations.csv)
└── plots/
    ├── box.png                  # distribution per metric
    ├── mean_stddev.png          # mean ± stddev bars
    ├── phase_stacked.png        # T0..T4 breakdown per iteration
    ├── latency_vs_iteration.png # drift / warm-up effects
    └── cdf.png                  # CDF with p50/p90/p99 markers
```

Re-analyze or re-plot without re-running, and overlay multiple runs:

```bash
.venv/bin/python -m src.cli analyze results/<run_id>
.venv/bin/python -m src.cli plot    results/<run_id>

.venv/bin/python -m src.cli plot results/gke-run \
    --compare results/aks-run results/aks-byocni-run
```

### Deep Cilium capture (`--deep-cilium`)

Append `--deep-cilium` to any `run` command to exec into the cilium-agent
on each new node right after T3 fires and capture:

- `cilium status -o json --verbose` → per-phase **bootstrap** durations
  (`k8sInit`, `restoreState`, `bpfBase`, `ipam`, `proxyInit`, `total`),
  IPAM mode/health, kube-proxy-replacement mode, agent version.
- The agent's Prometheus endpoint → `cilium_endpoint_regeneration_time_stats_seconds`
  (avg per scope), `cilium_identity_count`, `cilium_bpf_map_pressure`.

Headline numbers are merged into `iterations.csv` as
`cilium_bootstrap_{total,k8s_init,restore,bpf_base,ipam,proxy}_s` and
`cilium_endpoint_regen_avg_s`. Raw artefacts land under
`results/<run_id>/iter-<NNN>/`. Adds ~2-5s per iteration; off by default.

## Architecture

```
src/
├── cli.py             argparse entrypoint (run|analyze|plot|clean)
├── config.py          YAML + dataclass config
├── runner.py          iteration loop
├── collectors.py      K8s watchers, T0..T4 capture, cordon helpers
├── records.py         IterationRecord + derived metrics
├── analysis.py        pandas aggregation -> CSV/MD/JSON
├── plotting.py        matplotlib charts + --compare overlay
├── providers/         cluster lifecycle per cloud (ClusterProvider Protocol)
│   ├── gke_autopilot.py
│   ├── gke_standard_dpv2.py
│   ├── aks_overlay_cilium.py
│   ├── aks_byocni.py
│   └── existing.py
└── cni/               CNI ready-signal probes
    ├── cilium_dpv2.py     # GKE Dataplane V2 (anetd)
    └── cilium_generic.py  # upstream Cilium (AKS managed / BYOCNI)
```

To add a provider, implement `ClusterProvider` in `src/providers/<name>.py`
(`create`, `get_credentials`, `delete`, `node_autoprovision_hint`,
`cni_probe`, optional `pre_iteration`/`post_iteration`), register it in
`src/providers/__init__.py`, and add a `CNIProbe` under `src/cni/` if the
agent's labels or ready signal differ. Runner, collectors, analysis, and
plotting are cloud-agnostic.

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Unit tests cover parsers, derived-metric math, aggregation, plotting from
synthetic fixtures, and mocked AKS provider invocations. Live AKS smoke test
is opt-in:

```bash
AKS_LIVE_TEST=1 .venv/bin/python -m src.cli run --provider aks_overlay_cilium --iterations 1
```

## Notes & caveats

- **Cross-platform comparability.** The measurement code path is identical
  across providers, but the *node provisioning trigger* is platform-specific:

  | Provider | What causes the new VM |
  |---|---|
  | `gke_autopilot` | Autopilot node auto-provisioning (no pre-existing pool) |
  | `gke_standard_dpv2` | GKE cluster-autoscaler scales pool from `min=0` |
  | `aks_overlay_cilium` / `aks_byocni` (`cluster_autoscaler`, default) | AKS cluster-autoscaler scales VMSS from `min=0` |
  | `aks_overlay_cilium` / `aks_byocni` (`nap`) | AKS Node Auto-Provisioning |
  | `aks_overlay_cilium` / `aks_byocni` (`manual`) | Harness scales nodepool +1 directly |

  As a result, `T1 − T0` is not strictly apples-to-apples: CA-driven runs
  include the autoscaler scan/decision time on top of cloud VM provisioning.
  For the most-comparable cross-cloud number, run AKS with
  `node_provisioning: nap` against `gke_autopilot`. `T4 − T1` (Ready after
  registration) and `T3 − T2` (Cilium init) are unaffected by the trigger
  mechanism and remain directly comparable across all providers.

- **Phase chart semantics.** The stacked bar in `phase_stacked.png` always
  sums to `node_startup_latency_s = T4 − T0`, split into *VM provision +
  node registered* (T0→T1) and *node init to Ready* (T1→T4). Cilium's init
  duration (T3 − T2) is overlaid as a black diamond line because it runs
  in **parallel** and on GKE Autopilot typically completes *after* T4.
  When `cni_induced_delay_s == 0` consistently, that is the intended
  finding, not a missing measurement.

- **T2/T3 capture path.** T2 and T3 are read from the agent Pod's status
  (`containerStatuses[].state.running.startedAt` and the `Ready` condition
  `lastTransitionTime`) — *not* from log scraping. This avoids a transient
  race on Autopilot where the konnectivity tunnel to a brand-new node isn't
  ready yet and `kubectl logs` returns `No agent available` for a few
  seconds (logs work normally once the node settles). Log scraping is kept
  as a fallback. If the agent Pod can't be located within the timeout,
  T2/T3 are emitted as `null` and the iteration still records T0/T1/T4
  (the primary KPI is always measured).

- **Replay.** `raw_events.jsonl` lets you re-run `analyze`/`plot` offline
  without re-provisioning clusters.
