"""CLI entrypoint."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import providers
from .analysis import write_outputs
from .config import Config
from .metadata import (append_summary_section, finalize_metadata,
                        gather_metadata, write_metadata)
from .plotting import plot_all, plot_compare
from .records import IterationRecord
from .runner import load_kube, run_iterations


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = Config.from_file(args.config)
    cfg.merge_cli(
        provider=args.provider,
        region=args.region,
        iterations=args.iterations,
        cluster_name=args.cluster_name,
    )
    if args.aks_node_provisioning is not None:
        cfg.aks.node_provisioning = args.aks_node_provisioning
    if args.aks_resource_group is not None:
        cfg.aks.resource_group = args.aks_resource_group
    if args.aws_region is not None:
        cfg.eks.region = args.aws_region
    if args.deep_cilium:
        cfg.cni.deep = True
    if args.capture_logs is not None:
        cfg.capture_logs = args.capture_logs
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(cfg.output.base_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Per-run isolation so two terminals can run in parallel without collisions:
    # 1. kubeconfig lives inside run_dir (unique by construction).
    # 2. cluster name is suffixed with run_id, unless the user passed --cluster-name.
    if cfg.kubeconfig_path is None:
        cfg.kubeconfig_path = (run_dir / "kubeconfig").resolve()
    if args.cluster_name is None and cfg.cluster_name_suffix is None:
        # Last 6 chars of run_id keep the cluster name within cloud length limits.
        cfg.cluster_name_suffix = run_id[-6:]
    if cfg.cluster_name_suffix:
        cfg.cluster_name = f"{cfg.cluster_name}-{cfg.cluster_name_suffix}"

    provider = providers.get(cfg.provider, cfg)
    if args.existing_cluster:
        cfg.cluster_name = args.existing_cluster
        provider = providers.get("existing", cfg)

    handle = provider.create(cfg)
    status = "failed"
    try:
        # Snapshot run identity + cluster facts BEFORE iterations so a partial
        # run still has metadata. Finalised in the `finally` block below.
        core = load_kube(handle.kubeconfig)
        # Probe apiserver reachability before any blocking call. Newly-created
        # AKS HCP clusters intermittently take several minutes for the public
        # apiserver endpoint to become routable from the harness host.
        from .runner import wait_for_apiserver
        if not wait_for_apiserver(core, timeout_s=600):
            raise RuntimeError(
                "apiserver did not become reachable within 10 minutes; "
                "aborting before iterations.")
        meta = gather_metadata(cfg=cfg, handle=handle, provider=provider,
                                core=core, run_id=run_id, cli_argv=sys.argv[1:])
        write_metadata(run_dir, meta)

        records: list[IterationRecord] = run_iterations(cfg, handle, provider, run_dir, run_id)
        summary = write_outputs(records, run_dir,
                                run_id=run_id, provider=provider.name, region=handle.region)
        plots = plot_all(run_dir / "iterations.csv", run_dir / "plots",
                         title=f"({provider.name} @ {handle.region})")
        logging.getLogger(__name__).info("wrote %d plots to %s", len(plots), run_dir / "plots")
        status = "success"
        print(f"\nRun {run_id} complete. Results in: {run_dir}")
        print(f"  iterations.csv  -> {run_dir/'iterations.csv'}")
        print(f"  summary.md      -> {run_dir/'summary.md'}")
        print(f"  plots/          -> {run_dir/'plots'}")
        return 0
    finally:
        finalized = finalize_metadata(run_dir, status=status)
        if finalized is not None:
            append_summary_section(run_dir / "summary.md", finalized)
        if not args.keep_cluster:
            provider.delete(handle)


def _cmd_analyze(args: argparse.Namespace) -> int:
    run_dir = Path(args.results_dir)
    csv = run_dir / "iterations.csv"
    if not csv.exists():
        print(f"no iterations.csv in {run_dir}", file=sys.stderr); return 2
    import pandas as pd
    df = pd.read_csv(csv)
    records = []
    for _, row in df.iterrows():
        # We re-emit summary from existing CSV without recomputing T*.
        pass
    # Simpler: write_outputs needs IterationRecord; instead recompute aggregate on the CSV directly.
    from .analysis import aggregate
    from tabulate import tabulate
    agg = aggregate(df)
    (run_dir / "summary.csv").write_text(agg.to_csv(index=False))
    print(tabulate(agg, headers="keys", tablefmt="github", showindex=False))
    return 0


def _cmd_plot(args: argparse.Namespace) -> int:
    from .report import resolve_runs
    if args.last is not None:
        base = Path(args.base_dir or "results")
        run_dirs = resolve_runs([], last=args.last, base_dir=base)
    elif args.results_dir:
        run_dirs = [Path(args.results_dir)]
    else:
        print("must provide results_dir or --last N", file=sys.stderr)
        return 2
    all_paths: list[Path] = []
    for run_dir in run_dirs:
        paths = plot_all(run_dir / "iterations.csv", run_dir / "plots",
                         iteration=args.iteration)
        print(f"wrote ({run_dir.name}):")
        for p in paths:
            print(f"  {p}")
        all_paths.extend(paths)
    # When --iteration is set we skip cross-run comparisons (per-iteration
    # profiling is a single-run drilldown).
    if args.iteration is not None:
        return 0
    # Comparison output:
    #   - explicit `--compare`: overlay the first run_dir against the listed extras
    #     (legacy behaviour; output lands in the primary run's plots/ dir)
    #   - implicit `--last N` with N>=2: compare all N selected runs and write
    #     into a shared, primary-less directory under <base>/analysis/
    if args.compare:
        base_run = run_dirs[0]
        csvs = [Path(d) / "iterations.csv" for d in args.compare]
        cmp_paths = plot_compare([base_run / "iterations.csv", *csvs], base_run / "plots")
        print("comparison:")
        for p in cmp_paths:
            print(f"  {p}")
    elif args.last is not None and len(run_dirs) >= 2:
        base = Path(args.base_dir or "results")
        # Name the output by the *newest* run id so it's stable and discoverable
        newest = max(run_dirs, key=lambda p: p.name).name
        out_dir = base / "analysis" / f"compare-last-{len(run_dirs)}-{newest}" / "plots"
        # newest first in the legend / cdf line order
        ordered = sorted(run_dirs, key=lambda p: p.name, reverse=True)
        csvs = [d / "iterations.csv" for d in ordered]
        cmp_paths = plot_compare(csvs, out_dir)
        print(f"comparison (--last {args.last}):")
        for p in cmp_paths:
            print(f"  {p}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from .report import build_report
    results_dir = Path(args.results_dir or "results")
    out_dir = Path(args.out_dir or "analysis")
    md, docx = build_report(args.run_ids, last=args.last,
                             results_dir=results_dir, out_dir=out_dir)
    print("wrote:")
    print(f"  {md}")
    print(f"  {docx}")
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    base = Path(args.base_dir or "results")
    removed = 0
    for child in base.iterdir():
        if child.is_dir() and child.name != ".gitkeep":
            import shutil; shutil.rmtree(child); removed += 1
    print(f"removed {removed} run dir(s) under {base}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="node-startup-latency",
                                description="Measure node startup latency across cloud providers.")
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="execute a measurement run")
    pr.add_argument("--config", default="config.yaml")
    pr.add_argument("--provider", default=None,
                    choices=["gke_autopilot", "gke_standard_dpv2",
                             "aks_overlay_cilium", "aks_byocni", "aks_kubenet",
                             "eks_eni_cilium",
                             "existing"])
    pr.add_argument("--region", default=None)
    pr.add_argument("--iterations", type=int, default=None)
    pr.add_argument("--cluster-name", default=None)
    pr.add_argument("--aks-node-provisioning", default=None,
                    choices=["cluster_autoscaler", "nap", "manual"],
                    help="AKS only: how new nodes are provisioned per iteration")
    pr.add_argument("--aks-resource-group", default=None,
                    help="AKS only: override the resource group name "
                         "(default: node-latency-rg from config.yaml)")
    pr.add_argument("--aws-region", default=None,
                    help="EKS only: override the AWS region "
                         "(default: top-level --region, then config.yaml)")
    pr.add_argument("--existing-cluster", default=None,
                    help="reuse current kubeconfig context with this cluster name")
    pr.add_argument("--keep-cluster", action="store_true")
    pr.add_argument("--run-id", default=None)
    pr.add_argument("--deep-cilium", action="store_true",
                    help="exec into cilium-agent after T3 to capture "
                         "`cilium status -o json --verbose` and Prometheus "
                         "metrics; adds bootstrap/regen columns to iterations.csv")
    pr.add_argument("--capture-logs", choices=["none", "minimal"], default=None,
                    help="capture pod logs into iter-NNN/logs/ for forensics. "
                         "'minimal' = cilium-agent + CNS/IPAM pods on the target "
                         "node, time-bounded to the iteration window. Default: none.")
    pr.set_defaults(func=_cmd_run)

    pa = sub.add_parser("analyze", help="re-aggregate stats from an existing run dir")
    pa.add_argument("results_dir")
    pa.set_defaults(func=_cmd_analyze)

    pp = sub.add_parser("plot", help="(re)generate plots for an existing run dir")
    pp.add_argument("results_dir", nargs="?", default=None,
                    help="run dir under results/ (omit when using --last)")
    pp.add_argument("--last", type=int, default=None,
                    help="(re)plot the last N runs by directory name; "
                         "when N>=2 also emit cross-provider compare plots "
                         "into <base>/analysis/compare-last-N-<newest>/plots/")
    pp.add_argument("--base-dir", default=None,
                    help="root of run directories when using --last (default: results/)")
    pp.add_argument("--compare", nargs="*", default=[],
                    help="additional run dirs to overlay on the comparison CDF")
    pp.add_argument("--iteration", type=int, default=None,
                    help="profile a single iteration (1-indexed) from the run; "
                         "emits only phase_profile_iter-NNN.png and skips "
                         "aggregate / compare plots")
    pp.set_defaults(func=_cmd_plot)

    pc = sub.add_parser("clean", help="remove all run dirs under results/")
    pc.add_argument("--base-dir", default=None)
    pc.set_defaults(func=_cmd_clean)

    prp = sub.add_parser("report",
                          help="generate an analysis report (md + docx) for one or more runs")
    prp.add_argument("run_ids", nargs="*",
                      help="specific run dir names under results/; "
                           "omit to use --last N")
    prp.add_argument("--last", type=int, default=None,
                      help="select the last N runs by directory name (timestamp-prefixed)")
    prp.add_argument("--results-dir", default=None,
                      help="root of run directories (default: results/)")
    prp.add_argument("--out-dir", default=None,
                      help="output directory for the report files (default: analysis/)")
    prp.set_defaults(func=_cmd_report)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
