# Copilot instructions — node-startup-latency

Measures K8s networking-wiring latency on managed K8s (GKE Autopilot, GKE
Standard DPv2, AKS Overlay+Cilium, AKS BYOCNI, AKS kubenet, EKS+Cilium ENI).
See `README.md` for methodology and the full T0..T5 / Ts / Tts marker scheme.

## Workflows

Use the venv:

```bash
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v             # full suite (64 tests, fully offline)
.venv/bin/python -m pytest tests/test_aks.py -v  # single file
.venv/bin/python -m pytest tests/test_basic.py::test_name -v   # single test
```

No linter / formatter is configured — match the existing style.

CLI entrypoint is always `python -m src.cli` (never `src/cli.py` directly):

```
python -m src.cli run     --provider <id> [...]   # provision cluster, run iterations, teardown
python -m src.cli analyze results/<run_id>        # re-aggregate stats from raw_events.jsonl
python -m src.cli plot    results/<run_id>        # re-plot; supports --compare / --last N
python -m src.cli report  <run_id> [<run_id>...]  # md + docx into analysis/, embeds phase_profile_report.png
python -m src.cli clean                           # wipe results/
```

`raw_events.jsonl` is the source of truth for offline replay; `analyze`/`plot`/
`report` must never need cloud credentials.

## Architecture

Pipeline (one direction, cloud-agnostic except providers):

`cli` → `config` → `providers/<id>` (cluster lifecycle) → `runner`
(iteration loop) → `collectors` (K8s watchers → `IterationRecord`) →
`records` (derived metrics) → `analysis` (CSV/MD/JSON) → `plotting` /
`report`.

Per-iteration enrichment (T1→T1c window) lives in `collectors.py`:
pod lifecycle, init containers, CSINode, taint, `node_pod_events_json`,
`node_container_starts_json`, `node_image_pulls_json`,
`node_pod_status_json`. The `image` field on container starts and the
`Pulled`-without-`Pulling` fallback in image pulls are both load-bearing
for synthetic pull markers — preserve them when touching the watchers.

### Adding a provider

Implement `ClusterProvider` (`src/providers/base.py`) in
`src/providers/<name>.py`: `create`, `get_credentials`, `delete`,
`node_autoprovision_hint`, `cni_probe`; optional `describe`,
`pre_iteration`, `post_iteration`. Register in `src/providers/__init__.py`.
For AKS variants subclass `AKSProviderBase` (`_aks_base.py`) and override
`_az_create_cluster_args` (+ optional `_post_create` for Helm CNIs).
If the agent's ready signal differs, add a `CNIProbe` under `src/cni/`
(`cilium_dpv2`, `cilium_generic`, `noop` for kubenet). Runner / collectors
/ analysis / plotting stay untouched.

## Project conventions

- **Headline KPI is `time_to_runnable_s = T5 − T1`**, reported as **p50**.
  Never fold `T1 − T0` (IaaS) into it. `aks_kubenet` has no agent DS, so
  T2/T3/`cilium_*` columns are `null` by design — keep them nullable
  through analysis and plotting.
- **Marker collapsing**: phase profile collapses any group of T-markers
  within 1 ms into combined labels (`T1=Ts`, `T4=T4b`). Don't reorder.
- **Pull → create → run invariant** (`src/plotting.py`): for every
  `(pod, container)` triple, `pull.start < create.start ≤ run.start`.
  A pre-sort pass shifts pull lanes backwards (preserving width) to
  enforce this on every provider. Touch with care.
- **`_CILIUM_LIFECYCLE_ACTORS`** in `plotting.py` auto-groups lanes
  (`image_pull`, `cni`, `cilium`, `kubelet_main`, `cilium_regen`,
  `init_run`) under `cilium_pod_key` when no explicit pod-key entry
  exists. Cilium-pod run lanes intentionally rely on this.
- **`pod_basename`** strips trailing `[a-z0-9]{5,10}` hash tokens
  iteratively (token has a digit or no vowels). Handles GKE's
  `-m-<hash>` discriminator.
- **`REPORT_PODS_BY_PROVIDER`** in `report.py` filters per-provider focus
  pods (`{anetd, netd}` for GKE, `{cilium}` for EKS, `{cilium, azure-cns}`
  for AKS Cilium, `{azure-cns}` for AKS kubenet). `PROVIDER_DISPLAY_NAMES`
  is the canonical env-shorthand map ("AKS Overlay+Cilium", etc.) used
  everywhere in report output. New providers must extend both.
- **Run IDs** are `YYYYMMDD-HHMMSS`; tests rely on this format.
- **Parallel safety**: each run owns `results/<run_id>/` and
  `results/<run_id>/kubeconfig`; cluster names get a 6-char run-id
  suffix unless `--cluster-name` is passed.
- **Report image generation is on-demand**: `RunData.phase_profile_png`
  regenerates `phase_profile_report.png` via `_plot_phase_profile(...,
  pods_filter=...)`; falls back to `phase_profile.png` on error.

## Useful pointers

- Configs / defaults: `config.yaml`. CLI flags override YAML.
- Synthetic test fixtures live in `tests/`; mocked AKS provider calls
  in `tests/test_aks.py` are the template for new-provider tests.
- Sample real runs to read when changing plotting/report:
  `results/20260526-142343` (GKE), `results/20260525-091416` (EKS),
  `results/20260522-155452` (AKS+Cilium).
- `docs/analysis-prompt.md` is the *legacy* prompt-driven analysis
  workflow; the deterministic `report` subcommand replaces it. Prefer
  the latter and update it when adding fields.
