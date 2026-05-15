"""Programmatic run-comparison report generator.

Replaces the prompt-based workflow (`docs/analysis-prompt.md`) with a CLI:

    python -m src.cli report --last 2
    python -m src.cli report 20260514-164726 20260514-164732

Produces a Markdown file and a matching .docx in `./analysis/` (repo root).
Each run's `phase_profile.png` is embedded at the top so the reader can
visually compare critical-path shape before reading the numeric tables.
"""
from __future__ import annotations

import datetime as _dt
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

# Cilium configmap keys to diff across runs (subset of analysis-prompt.md).
CILIUM_DIFF_KEYS = [
    "ipam", "routing-mode", "tunnel", "tunnel-protocol",
    "kube-proxy-replacement", "cni-chaining-mode",
    "identity-management-mode", "identity-allocation-mode",
    "enable-bandwidth-manager", "enable-bpf-masquerade",
    "enable-endpoint-routes", "enable-host-legacy-routing",
    "enable-ipv4-masquerade", "enable-l7-proxy",
    "prometheus-serve-addr", "operator-prometheus-serve-addr",
    "datapath-mode", "bpf-lb-mode", "bpf-lb-algorithm",
    "agent-not-ready-taint-key", "set-cilium-node-taints",
]

# Metrics to surface in the KPI table (order matters).
KPI_METRICS = [
    ("node_ready_after_register_s",   "node_ready_after_register_s (mean)  *(autoscaler-free)*"),
    ("node_register_latency_s",       "node_register_latency_s (mean)      *(autoscaler + VM bringup)*"),
    ("node_startup_latency_s",        "node_startup_latency_s (mean / p90)"),
    ("time_to_schedulable_s",         "time_to_schedulable_s (mean / p90)"),
    ("cni_conflist_install_s",        "cni_conflist_install_s (mean / p90)"),
    ("post_conflist_ready_s",         "post_conflist_ready_s (mean)"),
    ("cilium_init_duration_s",        "cilium_init_duration_s (mean)"),
    ("cni_induced_delay_s",           "cni_induced_delay_s (mean)"),
    ("cilium_scheduling_block_s",     "cilium_scheduling_block_s (mean / p90)"),
    ("cilium_bootstrap_total_s",      "bootstrap.overall (mean, range)"),
    ("cilium_bootstrap_bpf_base_s",   "bootstrap.bpf_base (mean)"),
    ("cilium_endpoint_regen_avg_s",   "endpoint_regen_avg (mean)"),
]

# Anomaly event kinds worth surfacing (count > 0 → mention).
ANOMALY_KINDS = [
    "node_watch_reopen", "node_ready_watch_reopen",
    "cilium_deep_collect_failed",
    "node_schedulable_timeout",
    "uncordon_error",
]


@dataclass
class RunData:
    run_id: str
    run_dir: Path
    iterations: pd.DataFrame
    metadata: dict[str, Any]
    cilium_cm: dict[str, str] = field(default_factory=dict)
    cilium_summary: dict[str, Any] = field(default_factory=dict)
    event_counts: Counter = field(default_factory=Counter)
    missing: list[str] = field(default_factory=list)

    @property
    def provider(self) -> str:
        return (self.metadata.get("config") or {}).get("provider") or "unknown"

    @property
    def region(self) -> str:
        return (self.metadata.get("cluster") or {}).get("region") or "—"

    @property
    def k8s_version(self) -> str:
        return (self.metadata.get("cluster") or {}).get("kubernetes_version") or "—"

    @property
    def node_sku(self) -> str:
        nodes = (self.metadata.get("cluster") or {}).get("nodes") or []
        for n in nodes:
            labels = n.get("labels") or {}
            sku = labels.get("node.kubernetes.io/instance-type") \
                or labels.get("beta.kubernetes.io/instance-type")
            if sku:
                return sku
        return "—"

    @property
    def label(self) -> str:
        bits = [self.provider, self.region]
        argv = self.metadata.get("cli_argv") or []
        if "--aks-node-provisioning" in argv:
            try:
                bits.append(argv[argv.index("--aks-node-provisioning") + 1])
            except IndexError:
                pass
        return f"{self.run_id} ({', '.join(bits)})"

    @property
    def phase_profile_png(self) -> Path | None:
        p = self.run_dir / "plots" / "phase_profile.png"
        return p if p.exists() else None

    @property
    def ok(self) -> pd.DataFrame:
        if "status" not in self.iterations.columns:
            return self.iterations
        return self.iterations[self.iterations["status"] == "success"]


def _augment_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Backfill derived metrics for older iterations.csv files predating them."""
    if df.empty:
        return df
    def _delta(end: str, start: str) -> pd.Series:
        if end not in df.columns or start not in df.columns:
            return pd.Series([pd.NA] * len(df))
        return (pd.to_datetime(df[end], utc=True, errors="coerce")
                - pd.to_datetime(df[start], utc=True, errors="coerce")).dt.total_seconds().clip(lower=0)
    derived = {
        "node_ready_after_register_s": ("T4_node_ready", "T1_node_registered"),
        "cni_conflist_install_s":      ("T1c_cni_conflist", "T1_node_registered"),
        "post_conflist_ready_s":       ("T4_node_ready", "T1c_cni_conflist"),
        "cilium_scheduling_block_s":   ("T4b_schedulable", "T4_node_ready"),
        "time_to_schedulable_s":       ("T4b_schedulable", "T0_pod_created"),
    }
    for col, (end, start) in derived.items():
        if col not in df.columns or pd.to_numeric(df[col], errors="coerce").isna().all():
            s = _delta(end, start)
            if not s.isna().all():
                df[col] = s
    return df


def load_run(run_dir: Path) -> RunData:
    rd = RunData(run_id=run_dir.name, run_dir=run_dir,
                 iterations=pd.DataFrame(), metadata={})

    csv = run_dir / "iterations.csv"
    if csv.exists():
        rd.iterations = _augment_derived(pd.read_csv(csv))
    else:
        rd.missing.append("iterations.csv")

    md = run_dir / "run_metadata.json"
    if md.exists():
        try:
            rd.metadata = json.loads(md.read_text())
        except Exception:
            rd.missing.append("run_metadata.json (unparseable)")
    else:
        rd.missing.append("run_metadata.json")

    cm = run_dir / "cilium_config" / "cilium-config.json"
    if cm.exists():
        try:
            rd.cilium_cm = json.loads(cm.read_text()).get("data") or {}
        except Exception:
            rd.missing.append("cilium-config.json (unparseable)")
    else:
        rd.missing.append("cilium_config/cilium-config.json")

    cs = run_dir / "cilium_config" / "summary.json"
    if cs.exists():
        try:
            rd.cilium_summary = json.loads(cs.read_text())
        except Exception:
            pass

    re = run_dir / "raw_events.jsonl"
    if re.exists():
        with re.open() as f:
            for line in f:
                try:
                    rd.event_counts[json.loads(line).get("kind", "?")] += 1
                except Exception:
                    continue
    else:
        rd.missing.append("raw_events.jsonl")

    return rd


def resolve_runs(run_ids: list[str], *, last: int | None,
                  base_dir: Path) -> list[Path]:
    if run_ids:
        out: list[Path] = []
        for rid in run_ids:
            d = base_dir / rid
            if not d.is_dir():
                raise FileNotFoundError(f"no run dir: {d}")
            out.append(d)
        return out
    if last is None or last <= 0:
        raise ValueError("either run_ids or --last N must be provided")
    candidates = sorted(
        (p for p in base_dir.iterdir()
         if p.is_dir() and p.name not in {".gitkeep", "analysis"}
         and (p / "iterations.csv").exists()),
        key=lambda p: p.name,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"no run dirs with iterations.csv under {base_dir}")
    return list(reversed(candidates[:last]))


# ---------- KPI computation ----------

def _series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors="coerce").dropna()


def _fmt(val: float | None, digits: int = 2) -> str:
    if val is None or pd.isna(val):
        return "—"
    return f"{val:.{digits}f}"


def _kpi_cell(s: pd.Series, *, mode: str) -> str:
    if s.empty:
        return "—"
    if mode == "mean":
        return _fmt(s.mean())
    if mode == "mean_p90":
        return f"{_fmt(s.mean())} / {_fmt(s.quantile(0.9))}"
    if mode == "mean_range":
        return f"{_fmt(s.mean())} ({_fmt(s.min())} – {_fmt(s.max())})"
    return _fmt(s.mean())


def _kpi_row(metric: str, runs: list[RunData]) -> list[str]:
    mode = "mean"
    if metric in {"node_startup_latency_s", "time_to_schedulable_s",
                   "cni_conflist_install_s", "cilium_scheduling_block_s"}:
        mode = "mean_p90"
    if metric == "cilium_bootstrap_total_s":
        mode = "mean_range"
    return [_kpi_cell(_series(r.ok, metric), mode=mode) for r in runs]


def build_kpi_table(runs: list[RunData]) -> list[list[str]]:
    header = ["Metric"] + [r.label for r in runs]
    rows: list[list[str]] = [header]

    # static cluster-info rows
    rows.append(["provider / region"] + [f"{r.provider} / {r.region}" for r in runs])
    rows.append(["node SKU"] + [r.node_sku for r in runs])
    rows.append(["k8s version"] + [r.k8s_version for r in runs])
    rows.append(["iterations (ok / total)"] +
                [f"{(r.iterations['status'] == 'success').sum() if 'status' in r.iterations.columns else len(r.iterations)} / {len(r.iterations)}"
                 for r in runs])

    for col, label in KPI_METRICS:
        cells = _kpi_row(col, runs)
        if all(c == "—" for c in cells):
            continue
        rows.append([label] + cells)
    return rows


# ---------- Cilium config diff ----------

def cilium_config_diff(runs: list[RunData]) -> list[str]:
    if not any(r.cilium_cm for r in runs):
        return ["_No `cilium_config/cilium-config.json` available for any run._"]
    lines: list[str] = []
    only_in: dict[int, list[str]] = {i: [] for i in range(len(runs))}
    diffs: list[str] = []
    identical: list[str] = []
    for key in CILIUM_DIFF_KEYS:
        vals = [r.cilium_cm.get(key) for r in runs]
        present = [(i, v) for i, v in enumerate(vals) if v is not None]
        if not present:
            continue
        if len(present) == 1:
            i, v = present[0]
            only_in[i].append(f"`{key}` = `{v}`")
            continue
        # check equality among present values
        unique = {v for _, v in present}
        if len(unique) == 1 and len(present) == len(runs):
            identical.append(f"`{key}={present[0][1]}`")
        else:
            parts = " vs ".join(f"{runs[i].run_id}=`{v}`" for i, v in present)
            diffs.append(f"`{key}`: {parts}")

    for i, items in only_in.items():
        if items:
            lines.append(f"- **{runs[i].run_id} only:** " + "; ".join(items))
    for d in diffs:
        lines.append(f"- **different value:** {d}")
    if identical:
        lines.append("- **Identical across runs:** " + ", ".join(identical))

    # image tags
    img_lines: list[str] = []
    for r in runs:
        at = (r.cilium_summary.get("agent_template") or {}).get("containers") or []
        ot = (r.cilium_summary.get("operator_template") or {}).get("containers") or []
        if not at and not ot:
            continue
        agent = next((f"{c['name']}: `{c.get('image','—')}`" for c in at if c.get("name") == "cilium-agent"), None)
        operator = next((f"{c['name']}: `{c.get('image','—')}`" for c in ot if c.get("name") == "cilium-operator"), None)
        bits = [b for b in (agent, operator) if b]
        if bits:
            img_lines.append(f"- **{r.run_id}:** " + "; ".join(bits))
    if img_lines:
        lines.append("")
        lines.append("**Container images:**")
        lines.extend(img_lines)

    return lines or ["_No diffable keys present._"]


# ---------- Anomalies ----------

def anomalies(runs: list[RunData]) -> list[str]:
    lines: list[str] = []
    for r in runs:
        bits: list[str] = []
        if r.iterations.empty:
            bits.append("no iterations.csv")
        else:
            n_total = len(r.iterations)
            n_ok = int((r.iterations.get("status") == "success").sum()) if "status" in r.iterations.columns else n_total
            if n_ok < n_total:
                bits.append(f"{n_total - n_ok} iteration(s) did not succeed")
        for kind in ANOMALY_KINDS:
            c = r.event_counts.get(kind, 0)
            if c:
                bits.append(f"{c}× `{kind}`")
        if bits:
            lines.append(f"- **{r.run_id}:** " + "; ".join(bits))
    return lines or ["_No anomalies detected._"]


# ---------- Auto-headline ----------

def headline(runs: list[RunData]) -> str:
    if len(runs) < 2:
        r = runs[0]
        for metric in ("node_ready_after_register_s", "node_startup_latency_s"):
            s = _series(r.ok, metric)
            if not s.empty:
                return (
                    f"Single-run report for `{r.run_id}` ({r.provider}, {r.region}). "
                    f"Mean `{metric}` = {_fmt(s.mean())} s (p90 {_fmt(s.quantile(0.9))} s)."
                )
        return f"Single-run report for `{r.run_id}` ({r.provider}, {r.region})."

    # Pick the best available headline metric (autoscaler-free preferred).
    metric = None
    for cand in ("node_ready_after_register_s", "node_startup_latency_s"):
        if all(not _series(r.ok, cand).empty for r in runs):
            metric = cand
            break
    if metric is None:
        return "Comparison report across the supplied runs."

    means = [_series(r.ok, metric).mean() for r in runs]
    fastest = min(range(len(runs)), key=lambda i: means[i] if pd.notna(means[i]) else float("inf"))
    slowest = max(range(len(runs)), key=lambda i: means[i] if pd.notna(means[i]) else -1.0)
    if fastest == slowest or pd.isna(means[fastest]) or pd.isna(means[slowest]):
        return "Comparison report across the supplied runs."
    delta = means[slowest] / means[fastest] if means[fastest] > 0 else float("inf")

    # Secondary: CNI conflist install delta if available
    cni = []
    for r in runs:
        s = _series(r.ok, "cni_conflist_install_s")
        cni.append(s.mean() if not s.empty else None)
    cni_note = ""
    if all(v is not None for v in cni) and cni[fastest] and cni[slowest]:
        cni_note = (f" CNI conflist install (`cni_conflist_install_s`): "
                    f"{_fmt(cni[fastest])} s vs {_fmt(cni[slowest])} s.")

    qualifier = " (autoscaler-free)" if metric == "node_ready_after_register_s" else ""
    return (
        f"On `{metric}`{qualifier}, **`{runs[fastest].run_id}` "
        f"({runs[fastest].provider}) is fastest at {_fmt(means[fastest])} s mean**, "
        f"vs `{runs[slowest].run_id}` ({runs[slowest].provider}) at "
        f"{_fmt(means[slowest])} s (~{delta:.1f}×).{cni_note} "
        f"See the per-run `phase_profile.png` plots below for the phase decomposition."
    )


# ---------- Rendering ----------

def _table_md(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header, *body = rows
    width = len(header)
    out = ["| " + " | ".join(header) + " |"]
    out.append("|" + "|".join(["---"] * width) + "|")
    for row in body:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def render_markdown(runs: list[RunData], out_path: Path) -> Path:
    when = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    run_ids = ", ".join(r.run_id for r in runs)
    title = ("Run Analysis — " + " vs ".join(r.run_id for r in runs)
             if len(runs) > 1 else f"Run Report — {runs[0].run_id}")

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"_Generated: {when}_  ")
    lines.append(f"_Runs analyzed: {run_ids}_")
    lines.append("")

    lines.append("## Phase profile")
    lines.append("")
    lines.append("Per-run mean phase Gantt (autoscaler/VM bringup shown as a number "
                 "in the subtitle so the post-T1 phases are readable).")
    lines.append("")
    for r in runs:
        p = r.phase_profile_png
        if p is None:
            lines.append(f"- _{r.run_id}: no `plots/phase_profile.png` (run with the harness ≥ T1c)._")
            continue
        # Markdown image path relative to the report file's parent directory:
        try:
            rel = Path("..") / p.resolve().relative_to(out_path.resolve().parent.parent)
        except ValueError:
            rel = p.resolve()
        lines.append(f"**{r.label}:**")
        lines.append("")
        lines.append(f"![phase_profile {r.run_id}]({rel.as_posix()})")
        lines.append("")

    lines.append("## Headline")
    lines.append("")
    lines.append(headline(runs))
    lines.append("")

    lines.append("## KPI table")
    lines.append("")
    lines.append(_table_md(build_kpi_table(runs)))
    lines.append("")

    lines.append("## Cilium config diff")
    lines.append("")
    lines.extend(cilium_config_diff(runs))
    lines.append("")

    lines.append("## Anomalies")
    lines.append("")
    lines.extend(anomalies(runs))
    lines.append("")

    caveats: list[str] = []
    for r in runs:
        for m in r.missing:
            caveats.append(f"- `{r.run_id}`: missing `{m}` — affected sections may be partial.")
    if caveats:
        lines.append("## Caveats")
        lines.append("")
        lines.extend(caveats)
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    return out_path


def render_docx(runs: list[RunData], out_path: Path) -> Path:
    from docx import Document
    from docx.shared import Inches, Pt

    doc = Document()
    when = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    title = ("Run Analysis — " + " vs ".join(r.run_id for r in runs)
             if len(runs) > 1 else f"Run Report — {runs[0].run_id}")
    doc.add_heading(title, level=0)
    doc.add_paragraph(f"Generated: {when}").italic = True
    doc.add_paragraph("Runs analyzed: " + ", ".join(r.run_id for r in runs)).italic = True

    # Phase profile images first
    doc.add_heading("Phase profile", level=1)
    doc.add_paragraph(
        "Per-run mean phase Gantt. The T0→T1 autoscaler/VM bringup is shown as "
        "a number in each plot's subtitle so the post-T1 lifecycle is readable."
    )
    for r in runs:
        doc.add_heading(r.label, level=2)
        p = r.phase_profile_png
        if p is None:
            doc.add_paragraph(f"(no phase_profile.png for {r.run_id})")
        else:
            doc.add_picture(str(p), width=Inches(6.5))

    doc.add_heading("Headline", level=1)
    doc.add_paragraph(headline(runs))

    doc.add_heading("KPI table", level=1)
    rows = build_kpi_table(runs)
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Light Grid Accent 1"
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            table.rows[i].cells[j].text = cell
            for run in table.rows[i].cells[j].paragraphs[0].runs:
                run.font.size = Pt(9)
                if i == 0:
                    run.bold = True

    doc.add_heading("Cilium config diff", level=1)
    for line in cilium_config_diff(runs):
        if line.startswith("- "):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif line.startswith("**"):
            p = doc.add_paragraph()
            p.add_run(line.strip("*")).bold = True
        elif line.strip() == "":
            continue
        else:
            doc.add_paragraph(line)

    doc.add_heading("Anomalies", level=1)
    for line in anomalies(runs):
        if line.startswith("- "):
            doc.add_paragraph(line[2:], style="List Bullet")
        else:
            doc.add_paragraph(line)

    caveats: list[str] = []
    for r in runs:
        for m in r.missing:
            caveats.append(f"{r.run_id}: missing {m}")
    if caveats:
        doc.add_heading("Caveats", level=1)
        for c in caveats:
            doc.add_paragraph(c, style="List Bullet")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def output_basename(runs: list[RunData]) -> str:
    if len(runs) == 1:
        return f"report-{runs[0].run_id}"
    if len(runs) == 2:
        return f"compare-{runs[0].run_id}-vs-{runs[1].run_id}"
    return f"compare-{runs[0].run_id}-plus{len(runs)-1}"


def build_report(run_ids: list[str], *, last: int | None,
                 results_dir: Path, out_dir: Path) -> tuple[Path, Path]:
    """Resolve runs, build both .md and .docx in out_dir, return paths."""
    run_dirs = resolve_runs(run_ids, last=last, base_dir=results_dir)
    runs = [load_run(d) for d in run_dirs]
    base = output_basename(runs)
    md_path = render_markdown(runs, out_dir / f"{base}.md")
    docx_path = render_docx(runs, out_dir / f"{base}.docx")
    return md_path, docx_path
