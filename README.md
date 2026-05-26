# node-startup-latency

Measures **K8s networking wiring latency** on managed Kubernetes — the
time from a fresh node registering with the apiserver to a workload pod
transitioning to `Running` — across GKE Autopilot, GKE Standard with
Dataplane V2, AKS with Azure CNI Powered by Cilium, AKS BYOCNI + upstream
Cilium, AKS kubenet, and EKS with Cilium in ENI mode.

**Headline KPI:** `time_to_runnable_s = T5_pod_running − T1_node_registered`
— the T1-anchored, IaaS-noise-free measure of CNI + kubelet + cilium-agent
+ IPAM wiring on a brand-new node. Cloud-side autoscaler + VM bringup
(T0 → T1) varies 5×–10× by warm-pool state / region / trigger mechanism
and is reported separately as `node_register_latency_s`, never folded
into the headline. Quote results as **median (p50)** over ≥10 iterations
— the IaaS cold-pool tail makes mean+stddev misleading.

## Methodology

Each iteration submits a single resource-heavy trigger Pod with
`podAntiAffinity` against earlier iterations, forcing the platform to
provision a brand-new node VM. The harness watches the Kubernetes API
and captures **lifecycle markers** from a common cluster clock: T0 Pod
created, T1 Node registered, T1c CNI conflist placed, T2 CNI agent
container started, T3 CNI agent Ready, T4 Node `Ready=True`, T4b Node
schedulable (taints cleared), T5 trigger pod first container Running —
plus per-iteration enrichment of the T1→T1c window (scheduler latency,
image-pull window, CSINode registration, blocking-taint observation,
per-init-container durations). The same code path runs across all six
providers; only the cluster-creation primitive and the new-node trigger
mechanism (Autopilot / NAP / cluster-autoscaler / manual scale) differ.

### Timestamps and derived metrics

Each timestamp captures a specific transition in the lifecycle of a brand-new
node. The table below names the marker, its concrete source, and — critically —
**what's inside** the interval before it (i.e. which cloud / kubelet / CNI work
contributes to the time from the previous marker to this one).

| Marker | Source | What's inside the interval ending here |
|---|---|---|
| **T0** Pod created            | `Pod.metadata.creationTimestamp` | — (start) |
| **T1** Node registered        | new `Node` object's `metadata.creationTimestamp`, first observed via watch | **Cloud-side autoscaler decision + VM allocation + VM boot + kubelet process startup**, ending when kubelet POSTs the Node to the apiserver. *Not* a measure of CNI or kubelet bootstrap. |
| **Tt** Blocking taint first observed | first watch event where the new Node carries a `NoSchedule` taint such as `node.cilium.io/agent-not-ready` (`T_taint_observed`) | Operator-stamping latency for the scheduling-block taint (AKS managed Cilium / BYOCNI only; absent on GKE DPv2 and AKS kubenet). |
| **Ts** Cilium agent Pod scheduled | `Pod.status.conditions[PodScheduled].lastTransitionTime` for the new node's **cilium-agent DS** pod — `cilium-agent` / `anetd` / `azure-cns` depending on the provider (`T_pod_scheduled`) | Scheduler queue time for the **infrastructure / networking** pod after the node registered. Drives the `T1 → Ts` "Scheduler latency" lane. Typically lands within a few ms of T1 (the scheduler picks the already-pending pod the moment the new node passes predicates), so the plot collapses both markers into a combined `T1=Ts` label when they coincide. |
| **Tips / Tip** Cilium agent image pull window | pod-scoped `Pulling` / `Pulled` Events for the cilium-agent container (`T_image_pull_start`, `T_image_pulled`) | Image pull duration for the agent container, measured on the new node. |
| **Tcsi** CSINode registered   | first watch event where the Node's `Ready=False` message no longer carries `CSINode is not yet initialized` (`T_csinode_ready`) | kubelet's CSINode CRD registration after node creation — frequently the dominant gating signal in the T1→T1c window on AKS. |
| **T1c** CNI conflist placed   | first watch event at which `Node.status.conditions[Ready].message` no longer reports `NetworkPluginNotReady` / `cni config uninitialized`. Captured at observation time (`utcnow()`) since the condition message carries no per-message timestamp. | **CNI plugin work to place a conflist in `/etc/cni/net.d/`** — Cilium's `install-cni` init container (managed/BYOCNI/DPv2) or the kubelet itself (kubenet). This is the *only* CNI-side signal that actually blocks `Node Ready=True` on a vanilla kubelet. |
| **T2** CNI agent container started | `pod.status.containerStatuses[*].state.running.startedAt` for the Cilium agent container on the new node | Image pull + container creation for the Cilium agent Pod (does **not** gate Node Ready — kubelet only needs T1c, not a running agent). |
| **T3** CNI agent Ready        | agent Pod's `Ready` condition `lastTransitionTime` | Cilium agent internal bootstrap (BPF compile, k8s init, IPAM, endpoint restore) + readiness-probe lag. With `--deep-cilium`, decomposed further into `bpfCompilation`, `bpfWaitForELF`, `bpfLoadProg`, `waitingForLock`, `mapSync` per-phase means. |
| **T4** Node `Ready=True`      | `Node.status.conditions[Ready].lastTransitionTime` | Residual kubelet status sync after the conflist landed. Typically the next 10 s heartbeat tick, often coincident with T1c. |
| **T4b** Node schedulable      | first watch event after T4 with no CNI-applied `NoSchedule` taint (e.g. `node.cilium.io/agent-not-ready`) on `Node.spec.taints` | Time the operator-applied scheduling-block taint remains after Node Ready (AKS managed Cilium / BYOCNI only — zero on GKE DPv2 and AKS kubenet, so the plot collapses `T4=T4b` there). |
| **Tts** Trigger pod scheduled | `Pod.status.conditions[PodScheduled].lastTransitionTime` for the **latency-trigger workload** pod (`T_trigger_scheduled`) | Scheduler queue time for the **user workload** pod once the node is admissible (i.e. after T4b). Same definition as Ts but on a *workload* pod instead of the cilium DS pod, so it answers "how fast did the user's pod get bound once scheduling unblocked?" Opens the `Tts → T5` "Trigger pod sandbox / CNI ADD" lane. |
| **T5** Trigger pod Running    | trigger pod's first `containerStatuses[0].state.running.startedAt` — the moment kubelet successfully completed CNI ADD (sandbox wired with an IP) and started the pause container | **CNI ADD on a workload pod** (sandbox creation + IP allocation by cilium-agent IPAM). Together with T1 this is the K8s-networking-only window the harness was built to compare. On GKE Autopilot, T5 typically lands ~10-15 s *after* T4b because T4b only requires Node Ready, while T5 also requires cilium-agent IPAM to be up. |

Per-iteration init-container timings (`initc_<name>_s`) are also captured for
every init container on the Cilium agent pod (e.g. `install-cni-binaries`,
`mount-cgroup`, `clean-cilium-state`) so the T1→T1c window can be attributed
to specific install-cni steps.

> **`Ts` vs `Tts` — same condition, different pods.** Both come from a Kubernetes
> `PodScheduled=True` condition, but on two *different* pods:
> - **`Ts`** = the **cilium / system DS pod** (cilium-agent / anetd / azure-cns)
>   — answers "how fast did the scheduler bind the *infrastructure* pod once the
>   node registered?". Drives `T1 → Ts` ("Scheduler latency").
> - **`Tts`** = the **latency-trigger workload pod** — answers "how fast did the
>   scheduler bind the *user workload* pod once the node became admissible
>   (post-T4b)?". Drives `Tts → T5` ("Trigger pod sandbox / CNI ADD").
>
> Ordering on the timeline is `T1 → Ts → ... → T4b → Tts → T5`. The phase profile
> plot collapses any group of T-markers that land within 1 ms of each other into
> a combined label (e.g. `T1=Ts`, `T4=T4b`) so coincident markers don't overlap
> illegibly.

**Decomposition (T1-anchored, the recommended cross-provider view):**

```
time_to_runnable_s  =  T5 − T1
                    =  (T1c − T1)   +  (T2 − T1c)        +  (T3 − T2)            +  (T5 − T3)
                       CNI conflist    kubelet → agent     cilium init             trigger pod CNI ADD
                                                                                   + cilium IPAM
```

The first two terms are kubelet / CNI installer work, the third is the
cilium agent's own bootstrap, the fourth is the workload's CNI ADD wait —
the only phase that varies materially with IPAM mode (ENI on EKS, overlay
on AKS, GCE alias-IP on GKE). The T0-anchored view
(`node_startup_latency_s = T4 − T0`) is emitted for backwards compat but
its variance is dominated by autoscaler + VM bringup (`T1 − T0`,
reported as `node_register_latency_s`) and is **not directly comparable
across providers**.

Derived metrics (seconds):

- **`time_to_runnable_s          = T5  − T1`** — **HEADLINE.** Cross-provider K8s-networking wiring time. Quote as **median (p50)**.
- `sandbox_setup_s             = T5  − Tts` — CNI ADD + sandbox setup on the trigger pod (workload-side analogue of `cilium_bootstrap_ipam_s`).
- `T1c_s_from_T1` … `T5_s_from_T1` — T1-anchored versions of each marker, for clean post-T1 decomposition.
- `node_register_latency_s       = T1  − T0` — autoscaler + VM bringup + kubelet startup. Cloud-side only; quote alongside the headline so IaaS contribution is visible but separated.
- `node_ready_after_register_s   = max(T4  − T1, 0)` — IaaS-free `node_startup_latency_s`. Equals `cni_conflist_install_s + post_conflist_ready_s`.
- `cni_conflist_install_s        = max(T1c − T1, 0)` — the **real** CNI-induced Node-Ready delay: kubelet refuses `Ready=True` until at least one usable conflist appears in `/etc/cni/net.d/`. The first watch event where the `NetworkPluginNotReady` Ready-message clears is T1c; the next kubelet status sync (≈ T4) follows shortly after.
- `post_conflist_ready_s         = max(T4  − T1c, 0)` — residual kubelet readiness work after the conflist landed.
- `cilium_init_duration_s        = T3  − T2` — Cilium agent container start → Ready; does **not** gate Node Ready.
- `cni_induced_delay_s           = max(T4  − T3, 0)` — Cilium **agent Pod** gating Node Ready. ≈ 0 on every supported provider in practice, because kubelet only needs the conflist (T1c) not the running agent — see `cilium_scheduling_block_s` for the *real* pod-scheduling delay on AKS Cilium.
- `cilium_scheduling_block_s     = max(T4b − T4, 0)` — Cilium gating pod *scheduling* via the `node.cilium.io/agent-not-ready:NoSchedule` taint (operator stamps it; agent removes it once Ready). Non-zero on AKS managed Cilium / BYOCNI (run with `set-cilium-node-taints=true`); zero on GKE DPv2 and AKS kubenet (no equivalent taint), so `T4b == T4` there.
- `node_startup_latency_s        = T4  − T0` *(legacy end-to-end; **includes** autoscaler + VM provisioning)*
- `time_to_schedulable_s         = T4b − T0` *(legacy; same IaaS-dependence. **T4b is not "runnable"** — it only means the scheduler is willing to bind a pod; CNI ADD may still be pending. On GKE Autopilot T4b can fire before T1c. Use `T5` / `time_to_runnable_s` for the honest "node became useful" signal.)*

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Per-provider prerequisites:

| Providers | Required tools |
|---|---|
| `gke_autopilot`, `gke_standard_dpv2` | `gcloud` authenticated |
| `aks_overlay_cilium`, `aks_kubenet` | `az` logged in (`az login`), `kubectl` |
| `aks_byocni` | `az` logged in, `kubectl`, `helm` |
| `eks_eni_cilium` | `aws` configured (`aws configure` / env / IRSA), `eksctl`, `kubectl`, `helm` |
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

# AKS kubenet (legacy, no Cilium agent — T2/T3 emitted as null)
.venv/bin/python -m src.cli run --provider aks_kubenet       --region westeurope --iterations 10

# EKS with Cilium in ENI mode (replaces AWS VPC CNI; per Isovalent 2025/06/19 guide)
.venv/bin/python -m src.cli run --provider eks_eni_cilium    --aws-region us-east-1 --iterations 10

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

#### `aks_kubenet` notes

`aks_kubenet` reuses the same `aks:` config block as the Cilium-based AKS
providers but passes `--network-plugin kubenet` (no `--network-dataplane`,
no overlay flags). There is **no per-node CNI agent DaemonSet** under
kubenet, so the harness records T0/T1/T4 only and emits T2/T3 as `null`;
`cilium_init_duration_s` and `cni_induced_delay_s` will therefore also be
`null` in `iterations.csv`. The headline KPI (`time_to_runnable_s`)
and `node_register_latency_s` remain directly comparable to the
Cilium-based providers; `--deep-cilium` is a no-op for this provider.

Kubenet is deprecated in AKS and is rejected on newer Kubernetes
versions — set `aks.kubernetes_version` to a release where kubenet is
still accepted (e.g. `1.28`) before running.

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
├── iterations.csv         # per-iteration row (T0..T4b + decomposition columns + derived metrics)
├── summary.csv            # aggregate stats per metric
├── summary.md             # human-readable Markdown report
├── summary.json           # machine-readable summary
├── kubeconfig             # per-run isolated kubeconfig (parallel-safe)
├── iter-001/              # only with --deep-cilium: per-iteration Cilium artefacts
│   ├── cilium_metrics.txt        # raw Prometheus dump from the agent on the new node
│   ├── cilium_deep_headline.json # parsed headline numbers (also merged into iterations.csv)
│   └── scraper_probe.log         # logs from the single-shot scraper Pod
├── cilium_config/         # one-shot Cilium configuration snapshot (agent + operator)
│   ├── cilium-config.json        # full `cilium-config` ConfigMap
│   ├── agent_daemonset.json      # full anetd / cilium-agent DaemonSet spec
│   ├── operator_pod.json         # cilium-operator Pod
│   ├── operator_deployment.json  # cilium-operator Deployment spec
│   └── summary.json              # condensed: image tags, key flags, IPAM, KPR, port bindings
└── plots/
    ├── box.png                  # distribution per metric
    ├── mean_stddev.png          # mean ± stddev bars (kept; prefer p50 from summary.md)
    ├── phase_stacked.png        # two panels: top = T0→T1 (IaaS, high variance),
    │                            #            bottom = T1→T5 K8s networking
    │                            #            (T1c, T4, T4b, T5 breakdown — comparable across clouds)
    ├── phase_profile.png        # Gantt-style swimlane (kubelet / CNI / image-pull / cilium /
    │                            # scheduler) re-baselined to T1, with per-container pull / create / run
    │                            # lanes and per-phase `bootstrap:` and `regen:` lanes inlined.
    │                            # T-markers: T1, Tt, Ts, Tips, Tip, Tcsi, T1c, T2, T3, T4, T4b, Tts, T5;
    │                            # coincident markers (within 1 ms) collapse to combined labels (e.g. T1=Ts, T4=T4b).
    ├── latency_vs_iteration.png # both T0→T1 (IaaS) and T1→T5 (K8s networking) per iteration
    └── cdf.png                  # CDF of time_to_runnable_s (falls back to node_startup_latency_s
                                 # for older runs that pre-date T5 capture) with p50/p90/p99 markers
```

Re-analyze or re-plot without re-running, and overlay multiple runs:

```bash
.venv/bin/python -m src.cli analyze results/<run_id>
.venv/bin/python -m src.cli plot    results/<run_id>

# re-plot the last N runs in one go
.venv/bin/python -m src.cli plot --last 2

.venv/bin/python -m src.cli plot results/gke-run \
    --compare results/aks-run results/aks-byocni-run
```

Generate a programmatic comparison report (Markdown + Word .docx) under
`analysis/` at the repo root, embedding each run's `phase_profile.png` at the
top:

```bash
# specific runs
.venv/bin/python -m src.cli report 20260514-164726 20260514-164732

# last 2 runs (sorted by run-id timestamp)
.venv/bin/python -m src.cli report --last 2
```

This replaces the prompt-driven workflow in `docs/analysis-prompt.md` with a
deterministic build that computes the KPI table, Cilium configmap diff,
container image deltas, and anomaly counts from `raw_events.jsonl`. The
auto-generated headline is purely numeric — bring your own narrative if you
want prose inferences.

### Deep Cilium capture (`--deep-cilium`)

Append `--deep-cilium` to any `run` command to scrape the Cilium agent's
Prometheus `/metrics` endpoint on each new node right after T3 fires and
capture:

- **Bootstrap phase durations** from `cilium_bootstrap_seconds{scope=...}`
  — every published scope is captured, including `overall`, `earlyInit`,
  `k8sInit`, `daemonInit`, `ipam`, `mapsInit`, `bpfBase`, `restoreState`,
  `cleanup`, `fqdn`, `enableConntrack`, `healthCheck`.
- **Endpoint regeneration phases** from
  `cilium_endpoint_regeneration_time_stats_seconds{scope=...}` — average per
  scope (`bpfCompilation`, `bpfWaitForELF`, `bpfLoadProg`,
  `waitingForLock`, `mapSync`), plus `cilium_identity_count`,
  `cilium_bpf_map_pressure`, agent version.

Headline numbers are merged into `iterations.csv` as the
`cilium_bootstrap_{total,early_init,k8s_init,daemon_init,ipam,maps_init,bpf_base,restore,cleanup,fqdn,enable_conntrack,health_check}_s`
and `cilium_endpoint_regen_{avg,bpf_compilation,bpf_wait_for_elf,bpf_load_prog,waiting_for_lock,map_sync}_s`
columns, along with `cilium_identity_count` and `cilium_version`. Raw
artefacts land under `results/<run_id>/iter-<NNN>/`. Adds ~3-5s per
iteration; off by default.

**How it works (works on every supported platform, including GKE Autopilot).**
A single-shot scraper Pod (`curlimages/curl`) is created in the `default`
namespace, pinned to the new node via `nodeName` (no scheduler / no extra
node provisioned), and curls `http://<agent-pod-ip>:<port>/metrics`.
Because the Cilium agent runs `hostNetwork: true` everywhere (GKE `anetd`,
AKS Azure-CNI cilium, AKS BYOCNI cilium), its PodIP equals the node IP and
the metrics port is reachable from any same-node Pod — no `pods/exec`,
`pods/proxy`, `hostNetwork`, or privileged operations are needed, so
Autopilot's GKE Warden does not block it. The scraper Pod is deleted
after each iteration. Override the image / namespace via
`cni.deep_scraper_image` / `cni.deep_scraper_namespace` in `config.yaml`.

> **GKE prerequisite — automatic.** When `--deep-cilium` is set, the GKE
> providers create the cluster with `--enable-dataplane-v2-metrics`
> (and `--enable-dataplane-v2-flow-observability`). Without these flags
> `anetd` does not bind a TCP listener on port 9090 and the scraper sees
> connection-refused. AKS exposes the Cilium metrics port unconditionally,
> so no equivalent flag is needed there.

## Architecture

```
src/
├── cli.py             argparse entrypoint (run|analyze|plot|report|clean)
├── config.py          YAML + dataclass config
├── runner.py          iteration loop
├── collectors.py      K8s watchers, T0..T4b capture, T1→T1c enrichment (pod lifecycle, init containers, CSINode, taint)
├── records.py         IterationRecord + derived metrics
├── analysis.py        pandas aggregation -> CSV/MD/JSON
├── plotting.py        matplotlib charts + --compare overlay (phase_profile Gantt with inlined per-container, bootstrap, and regen lanes)
├── report.py          deterministic md + docx comparison report
├── cilium_deep.py     per-iteration Cilium /metrics scraper (bootstrap + regen phases)
├── cilium_config.py   one-shot Cilium DS / operator / configmap snapshot
├── metadata.py        run_metadata.json builder
├── providers/         cluster lifecycle per cloud (ClusterProvider Protocol)
│   ├── _aks_base.py        shared AKS create / credentials / delete plumbing
│   ├── _az.py              az CLI wrappers
│   ├── _cli.py             gcloud helpers (in-flight op wait, retry)
│   ├── _eks.py             eksctl / aws / helm / kubectl wrappers
│   ├── gke_autopilot.py
│   ├── gke_standard_dpv2.py
│   ├── aks_overlay_cilium.py
│   ├── aks_byocni.py
│   ├── aks_kubenet.py
│   ├── eks_eni_cilium.py
│   └── existing.py
└── cni/               CNI ready-signal probes
    ├── cilium_dpv2.py     # GKE Dataplane V2 (anetd)
    ├── cilium_generic.py  # upstream Cilium (AKS managed / BYOCNI)
    └── noop.py            # kubenet — no agent DaemonSet
```

To add a provider, implement `ClusterProvider` in `src/providers/<name>.py`
(`create`, `get_credentials`, `delete`, `node_autoprovision_hint`,
`cni_probe`, optional `describe`, `pre_iteration`/`post_iteration`), register
it in `src/providers/__init__.py`, and add a `CNIProbe` under `src/cni/` if
the agent's labels or ready signal differ. For AKS variants, subclass
`AKSProviderBase` from `_aks_base.py` and override `_az_create_cluster_args`
(plus optional `_post_create` for Helm-installed CNIs). Runner, collectors,
analysis, and plotting are cloud-agnostic.

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Unit tests cover parsers, derived-metric math, aggregation, plotting from
synthetic fixtures, mocked AKS provider invocations, and Cilium config /
deep-metrics parsing. All tests run fully offline against fakes — no cloud
credentials required. For a live smoke test on an actual cluster, point the
harness at it via the `existing` provider:

```bash
.venv/bin/python -m src.cli run --provider existing --iterations 1
```

## Notes & caveats

- **New-node trigger mechanism (varies per provider).** The measurement
  code path is identical across providers, but what causes the new VM
  differs — so `T1 − T0` is not strictly apples-to-apples:

  | Provider | What causes the new VM |
  |---|---|
  | `gke_autopilot` | Autopilot node auto-provisioning (no pre-existing pool) |
  | `gke_standard_dpv2` | GKE cluster-autoscaler scales pool from `min=0` |
  | `aks_*` (`cluster_autoscaler`, default) | AKS cluster-autoscaler scales VMSS from `min=0` |
  | `aks_*` (`nap`) | AKS Node Auto-Provisioning |
  | `aks_*` (`manual`) | Harness scales nodepool +1 directly |
  | `eks_eni_cilium` | EKS Cluster Autoscaler scales the `latencypool` ASG from `min=0` |

  Even within a single provider the T1 distribution can swing 5×–10×
  between warm-pool hits and cold node-pool creation (observed on GKE
  Autopilot: 7 s warm vs 170 s cold in the same run). `T4 − T1` (Ready
  after registration) and `T3 − T2` (Cilium init) are
  trigger-independent and directly comparable.

- **Phase chart semantics.** `phase_stacked.png` is a **two-panel** figure
  sharing the X axis: the top panel shows `T0 → T1` (IaaS — autoscaler + VM
  bringup) in grey, labelled as high-variance and not directly comparable
  across clouds; the bottom panel stacks the K8s networking phases
  `T1 → T1c` (CNI conflist install), `T1c → T4` (kubelet residual sync),
  `T4 → T4b` (Cilium scheduling-block taint), and `T4b → T5` (sandbox
  setup / CNI ADD on the trigger pod) — the directly comparable, IaaS-free
  view. Cilium's init duration (T3 − T2) runs in **parallel** with kubelet
  Ready and on GKE Autopilot typically completes *after* T4. When
  `cni_induced_delay_s == 0` consistently, that is the intended finding,
  not a missing measurement.

- **T2/T3 capture path.** T2 and T3 are read from the agent Pod's status
  (`containerStatuses[].state.running.startedAt` and the `Ready` condition
  `lastTransitionTime`) — *not* from log scraping. This avoids a transient
  race on Autopilot where the konnectivity tunnel to a brand-new node isn't
  ready yet and `kubectl logs` returns `No agent available` for a few
  seconds (logs work normally once the node settles). Log scraping is kept
  as a fallback. If the agent Pod can't be located within the timeout,
  T2/T3 are emitted as `null` and the iteration still records T0/T1/T4
  (the headline KPI is always measured).

- **Replay.** `raw_events.jsonl` lets you re-run `analyze`/`plot` offline
  without re-provisioning clusters.

## License

MIT — see [LICENSE](LICENSE).
