# node-startup-latency

Automation for the **node startup latency** test plan: measures the time from
triggering node provisioning to `Node Ready=True` on GKE Autopilot (Dataplane V2 /
Cilium), with a provider abstraction so the same harness can later run on AKS
(Azure CNI Powered by Cilium) and AKS BYOCNI + Cilium.

## What it measures

Per iteration, the harness records five timestamps:

| Marker | Source |
|---|---|
| **T0** Pod created (provisioning trigger) | `Pod.metadata.creationTimestamp` |
| **T1** Node registered | new `Node` first observed via watch (`creationTimestamp`) |
| **T2** CNI agent container started | `pod.status.containerStatuses[*].state.running.startedAt` |
| **T3** CNI agent reports ready | log line matching `ready_regex` (Cilium) |
| **T4** Node `Ready=True` | `Node.status.conditions[Ready].lastTransitionTime` |

Derived metrics (seconds):

- `node_startup_latency_s = T4 − T0` *(primary KPI)*
- `node_register_latency_s = T1 − T0`
- `cilium_init_duration_s = T3 − T2`
- `cni_induced_delay_s   = max(T4 − T3, 0)`

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Pre-reqs depending on provider:

- `gke_autopilot` / `gke_standard_dpv2`: `gcloud` CLI authenticated.
- `aks_overlay_cilium` / `aks_byocni`: stubs (raise `NotImplementedError`) —
  provision out-of-band and use `--provider existing` for now.
- `existing`: a working `kubeconfig` for the target cluster.

## Run

```bash
.venv/bin/python -m src.cli run \
    --provider gke_autopilot \
    --region europe-west1 \
    --iterations 10
```

Other providers:

```bash
# Re-use a cluster you already created
.venv/bin/python -m src.cli run --provider existing --iterations 5

# GKE Standard with Dataplane V2 (Cilium)
.venv/bin/python -m src.cli run --provider gke_standard_dpv2 --region europe-west1

# AKS with Azure CNI Powered by Cilium (managed dataplane)
.venv/bin/python -m src.cli run --provider aks_overlay_cilium --region westeurope

# AKS BYOCNI + upstream Cilium installed via Helm
.venv/bin/python -m src.cli run --provider aks_byocni --region westeurope
```

### AKS prerequisites

- `az` CLI logged in (`az login`) with rights to create resource groups and AKS clusters.
- `helm` (only for `aks_byocni`) and `kubectl` on `PATH`.
- Configure provider-specific settings under the `aks:` block in `config.yaml`:

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
    max_count: 10
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

Trigger modes:
- `cluster_autoscaler` (default): user pool starts at `node_count`, CA scales up to satisfy each trigger Pod.
- `nap`: enables AKS Node Auto-Provisioning; no user pool is added.
- `manual`: harness scales the user pool by +1 before each iteration and back down after.

Live AKS smoke test (opt-in):

```bash
AKS_LIVE_TEST=1 .venv/bin/python -m src.cli run --provider aks_overlay_cilium --iterations 1
```

> Cost note: each AKS run provisions a real cluster (and BYOCNI installs Cilium via Helm). Always let the harness `delete` the cluster, or pass `--keep-cluster` only for debugging.

## Outputs

Each run writes to `results/<run-id>/`:

```
results/20251001-120000/
├── raw_events.jsonl       # every watcher event for offline replay
├── iterations.csv         # per-iteration row (T0..T4 + derived metrics)
├── summary.csv            # aggregate stats per metric
├── summary.md             # human-readable Markdown report
├── summary.json           # machine-readable summary
└── plots/
    ├── box.png                  # distribution per metric
    ├── mean_stddev.png          # mean +/- stddev bars
    ├── phase_stacked.png        # T0..T4 breakdown per iteration
    ├── latency_vs_iteration.png # drift / warm-up effects
    └── cdf.png                  # CDF with p50/p90/p99 markers
```

### Re-analyze / re-plot without re-running

```bash
.venv/bin/python -m src.cli analyze results/<run-id>
.venv/bin/python -m src.cli plot    results/<run-id>

# Cross-provider comparison overlay
.venv/bin/python -m src.cli plot results/gke-run \
    --compare results/aks-run results/aks-byocni-run
```

## Architecture

```
src/
├── cli.py                  # argparse entrypoint (run|analyze|plot|clean)
├── config.py               # YAML + dataclass config
├── runner.py               # iteration loop
├── collectors.py           # K8s watchers + Cilium log scan
├── records.py              # IterationRecord + derived metrics
├── analysis.py             # pandas aggregation -> CSV/MD/JSON
├── plotting.py             # matplotlib charts + --compare
├── providers/              # cluster lifecycle per cloud
│   ├── base.py             #   ClusterProvider Protocol
│   ├── gke_autopilot.py
│   ├── gke_standard_dpv2.py
│   ├── aks_overlay_cilium.py  # stub
│   ├── aks_byocni.py          # stub
│   └── existing.py
└── cni/                    # Cilium "ready" signal detection
    ├── base.py
    ├── cilium_dpv2.py      # GKE Dataplane V2 (anetd)
    └── cilium_generic.py   # upstream Cilium (AKS managed / BYOCNI)
```

### Adding a provider

1. Create `src/providers/<name>.py` implementing `ClusterProvider`
   (`create`, `get_credentials`, `delete`, `node_autoprovision_hint`, `cni_probe`).
2. Register it in `src/providers/__init__.py`.
3. If the CNI ready-line differs, add a `CNIProbe` under `src/cni/`.

The runner, collectors, analysis, and plotting modules are cloud-agnostic, so
any new provider produces results in the same schema as existing ones.

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Unit tests cover the parsers, derived-metric math, aggregation, and end-to-end
emission of CSV / Markdown / JSON / plots from synthetic fixtures.

## Notes & caveats

- **Phase chart semantics**: the stacked bar always sums to
  `node_startup_latency_s = T4 − T0` and is split into
  *VM provision + node registered* (T0→T1) and *node init to Ready* (T1→T4).
  The Cilium agent's init duration (T3 − T2) is overlaid as a black diamond
  line because it is a **parallel** signal that on GKE Autopilot typically
  completes **after** T4 — i.e. CNI does not delay node readiness on
  Autopilot. When `cni_induced_delay_s == 0` consistently, that is the
  intended finding, not a missing measurement.
- **T2/T3 capture path**: T2 (CNI agent container started) and T3 (CNI ready)
  are read from the agent Pod's `status.containerStatuses[].state.running.startedAt`
  and `Ready` condition `lastTransitionTime` — *not* from log scraping. This
  avoids a transient race on Autopilot where the konnectivity tunnel to a
  brand-new node isn't ready yet, so `kubectl logs` against that node returns
  `No agent available` for a few seconds (logs work normally once the node
  settles). Log scraping is kept only as a fallback. If the agent Pod can't be
  located within the timeout, T2/T3 are emitted as `null` and the iteration
  still records T0/T1/T4 (node startup latency is always measured).
- **Forcing fresh nodes**: trigger pods request 1500m CPU / 2Gi RAM by default
  with strict `podAntiAffinity` against earlier iterations, so each iteration
  needs a brand-new node on Autopilot. Tune in `config.yaml` for other providers.
- **Cost**: each iteration creates a node VM. The pod is torn down between
  iterations and Autopilot reaps idle nodes; for GKE Standard the autoscaler
  is configured `min=0`.
- **Replay**: `raw_events.jsonl` lets you re-run `analyze`/`plot` offline
  without burning more cloud resources.
