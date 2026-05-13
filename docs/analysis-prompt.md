# Run Analysis Prompt

Use this file to instruct Copilot (or any agent with repo read access) to analyze
one or more runs produced by this harness. Typical invocation:

> "Use the instructions in `docs/analysis-prompt.md` to analyze the last 2 runs."
>
> "Follow `docs/analysis-prompt.md` and compare runs `20260513-093101` and `20260513-093122`."
>
> "Apply `docs/analysis-prompt.md` to all runs under `results/` from today."

The agent should resolve "last N runs" by sorting `results/*/` directories by name
(timestamp-prefixed `YYYYMMDD-HHMMSS`) descending and picking the top N.

---

## Instructions for the analyzing agent

Produce a concise comparison summary of the supplied run(s) with **special focus
on Cilium**. Read only the files listed below from each run directory. Do not
speculate beyond what the files support.

### Per-run inputs

For every run directory `results/<RUN_ID>/`:

1. **`summary.md`** and **`iterations.csv`** — extract per-iteration and aggregate:
   - `T0` (trigger pod created), `T1` (node added), `T2` (cilium start),
     `T3` (cilium ready), `T4` (node ready)
   - `node_startup_latency_s` (T4 − T0)
   - `cilium_init_duration_s` (T3 − T2)
   - `cni_induced_delay_s` (T4 − T3, clamped ≥ 0)
   - Compute mean, p50, p90 across iterations.

2. **`run_metadata.json`** — note:
   - provider, region, k8s version
   - node SKU / instance type (look under `cluster_facts.nodes[*].instance_type`)
   - CNI mode / flags applied at create time
   - CLI args used

3. **`cilium_config/configmap.json`** — diff these keys against the other run(s):
   ```
   ipam, routing-mode, tunnel, tunnel-protocol,
   kube-proxy-replacement, cni-chaining-mode,
   identity-management-mode, identity-allocation-mode,
   enable-bandwidth-manager, enable-bpf-masquerade,
   enable-endpoint-routes, enable-host-legacy-routing,
   enable-ipv4-masquerade, enable-l7-proxy,
   prometheus-serve-addr, operator-prometheus-serve-addr,
   datapath-mode, bpf-lb-mode, bpf-lb-algorithm
   ```

4. **`cilium_config/agent_daemonset.json`** — record:
   - agent container image + tag (e.g. `cilium:v1.18.7-gke1.35-gke.1`)
   - any provider-specific `args` / `env` differing from upstream

5. **`cilium_config/operator_deployment.json`** (if present) — operator image tag.

6. **`cilium_deep/*.json`** (only if `--deep-cilium` was used) — aggregate
   across iterations:
   - `cilium_bootstrap_seconds{scope="overall"}` → mean, range
   - `cilium_endpoint_regeneration_time_stats_seconds{scope="bpfCompilation|waitingForLock|mapSync|policyCalculation|total"}` → mean per scope
   - `cilium_agent_api_process_time_seconds` → p50 / p99 of the slowest endpoints
   - Note any iteration where the scraper failed (missing file or empty payload).

7. **`raw_events.jsonl`** — scan for anomalies only:
   - `node_watch_reopen` / `node_ready_watch_reopen` events
   - `cilium_deep_collect_failed`
   - Any iteration that did not reach `node_ready`

### Output destination

Write the final report to a Markdown file under `results/analysis/`:

- **2 runs:** `results/analysis/compare-<run_id_1>-vs-<run_id_2>.md`
- **N > 2 runs:** `results/analysis/compare-<earliest_run_id>-plus<N-1>.md`
- **Single run:** `results/analysis/report-<run_id>.md`

Create the `results/analysis/` directory if it doesn't exist. If the target file
already exists, overwrite it. After writing, print the absolute path of the
generated file as the last line of the chat response.

### Output format

Produce the report in exactly this structure. Keep it tight — numeric facts over
prose. Round seconds to 2 decimals. Use `—` for missing data.

The Markdown file should begin with a top-level heading and a metadata block:

```
# Run Analysis — <run_id_1> vs <run_id_2>

_Generated: <ISO-8601 UTC timestamp>_
_Runs analyzed: <run_id_1>, <run_id_2>, ..._

```

…followed by the sections below.

```
### Headline
2–3 sentences: primary KPI delta, which run is faster end-to-end, and whether
Cilium sits on the critical path.

### KPI table
| Metric                          | <run_id_1> | <run_id_2> | ... |
|---------------------------------|------------|------------|-----|
| provider / region               |            |            |     |
| node SKU                        |            |            |     |
| k8s version                     |            |            |     |
| iterations (ok / total)         |            |            |     |
| node_startup_latency_s (mean)   |            |            |     |
| node_startup_latency_s (p90)    |            |            |     |
| cilium_init_duration_s (mean)   |            |            |     |
| cni_induced_delay_s (mean)      |            |            |     |
| bootstrap.overall (mean, range) |            |            |     |
| bpfCompilation (mean)           |            |            |     |
| waitingForLock (mean)           |            |            |     |
| mapSync (mean)                  |            |            |     |

### Cilium config diff
Bullet list of **differing** flags only — skip identical ones. Group as:
- **<run_id_1>-only:** ...
- **<run_id_2>-only:** ...
- **different value:** `<key>`: `<run_id_1>=...` vs `<run_id_2>=...`

Also note image tag deltas (agent + operator).

### Inferences (3–5 bullets)
- What does the bootstrap / bpfCompilation delta imply about agent vs platform
  overhead?
- Is `cilium_init_duration_s` dominated by Cilium code (bootstrap.overall) or by
  image-pull / container-start / readiness-probe gap?
- Is Cilium on the node-ready critical path? (`cni_induced_delay_s > 0`?)
- Anomalies: failed iterations, scraper timeouts, watch reopens.
- Any config flag that plausibly explains a measured latency delta.

### Caveats
One line if a section was skipped because data is missing (e.g. `--deep-cilium`
not enabled, cilium_config/ absent for runs predating that feature).
```

### Constraints

- Use only data from the supplied run directories. Do not fetch external info.
- If a file is missing, state so in **Caveats** and skip that section — do not
  fabricate numbers.
- When comparing more than 2 runs, widen the table and keep the diff section
  pairwise relative to the first run.
- Do not include time/date estimates beyond what's in the run files.
