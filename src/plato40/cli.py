"""Command-line entry point.

``plato40 run <data_dir> --out decisions.jsonl``   write decisions for one dataset
``plato40 arbiter <data_dir> <bus.jsonl>... --out decisions.jsonl --verdicts v.jsonl``
                                                  merge lane proposals (docs/BUS.md)
``plato40 validate <decisions.jsonl> <data_dir>``  check contract and autonomy policy
``plato40 explain <trace.jsonl> [episode]``        why the pipeline acted or did not
``plato40 ui <decisions.jsonl> <trace.jsonl> --out console.html``  operations console page
``plato40 bus <data_dir> --lane <lane> --out <dir>``  one trigger lane through bus and arbiter
``plato40 audit <data_root> --out <dir>``          write the data audit report
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from plato40 import arbiter, bus, contract, lanes, llm_guard, pipeline, trace, ui
from plato40.load.reader import discover_datasets, load_dataset
from plato40.settings import Settings
from plato40.state import RoomStates

ROW_ORDER = {"signal": 0, "proposal": 1, "verdict": 2}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plato40", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="write decisions.jsonl for one dataset")
    run.add_argument("data_dir", type=Path, help="one dataset directory (with manifest.json)")
    run.add_argument("--out", type=Path, required=True, help="decisions.jsonl to write")
    run.add_argument("--trace", type=Path, help="also write the run trace (for explain)")
    run.add_argument(
        "--llm-cache",
        type=Path,
        help="phrase guest messages in the guest's language from this cache; "
        "PLATO40_LLM=on also calls the model on a cache miss",
    )
    run.add_argument(
        "--lanes",
        nargs="*",
        choices=sorted(lanes.registry()),
        help="bus lanes that replace the planner for their issue type (default: all); "
        "give none to use the planner only",
    )
    run.add_argument(
        "--no-follow-ups",
        action="store_true",
        help="skip the closed-loop follow-up decisions (verify.py)",
    )
    arb = sub.add_parser("arbiter", help="merge lane proposals from bus files into decisions")
    arb.add_argument("data_dir", type=Path, help="one dataset directory (with manifest.json)")
    arb.add_argument("bus", type=Path, nargs="+", help="lane bus files (docs/BUS.md)")
    arb.add_argument("--out", type=Path, required=True, help="decisions.jsonl to write")
    arb.add_argument("--verdicts", type=Path, required=True, help="verdict rows to write")
    arb.add_argument(
        "--replace",
        nargs="*",
        default=[],
        choices=sorted(bus.LANES),
        help="lanes whose pipeline decisions the bus proposals replace",
    )
    arb.add_argument("--llm-cache", type=Path, help="as for run")
    val = sub.add_parser("validate", help="check a decisions file against contract and policy")
    val.add_argument("decisions", type=Path, help="decisions.jsonl to check")
    val.add_argument("data_dir", type=Path, help="the dataset the decisions were made on")
    exp = sub.add_parser("explain", help="show why the pipeline acted or did not")
    exp.add_argument("trace", type=Path, help="trace written by run --trace")
    exp.add_argument("episode", nargs="?", help="episode ID or part of it; omit to list")
    page = sub.add_parser("ui", help="write the operations console as one HTML page")
    page.add_argument("decisions", type=Path, help="decisions.jsonl written by run")
    page.add_argument("trace", type=Path, help="trace written by run --trace")
    page.add_argument("--out", type=Path, required=True, help="HTML page to write")
    lane = sub.add_parser("bus", help="run one trigger lane through the bus and the arbiter")
    lane.add_argument("data_dir", type=Path, help="one dataset directory (with manifest.json)")
    lane.add_argument("--lane", choices=sorted(lanes.registry()), required=True, help="lane to run")
    lane.add_argument(
        "--out", type=Path, required=True, help="directory for <lane>.bus.jsonl and decisions"
    )
    lane.add_argument(
        "--llm-cache",
        type=Path,
        default=Path("cache/llm"),
        help="checked LLM answers (llm_guard); a miss uses the template",
    )
    audit = sub.add_parser("audit", help="write the data audit report")
    audit.add_argument(
        "data_root",
        type=Path,
        help="a dataset directory, or a directory containing several (quickstart, dev)",
    )
    audit.add_argument("--out", type=Path, required=True, help="report output directory")
    return parser


def _run(
    data_dir: Path,
    out: Path,
    trace_path: Path | None,
    llm_cache: Path | None,
    run_lanes: tuple[str, ...],
    follow_ups: bool,
) -> int:
    ds = load_dataset(data_dir)
    settings = Settings(
        data_root=data_dir,
        out_dir=out.parent,
        llm_cache=llm_cache,
        lanes=run_lanes,
        follow_ups=follow_ups,
    )
    result = pipeline.run(ds, settings)
    n = contract.write_decisions(result.decisions, out)
    print(trace.summary(ds, result))
    if result.verdicts:
        no = [v for v in result.verdicts if not v["accepted"]]
        print(
            f"lanes      {', '.join(settings.lanes)}: {len(result.verdicts)} proposals, "
            f"{len(no)} rejected"
        )
        for v in no:
            print(f"  rejected {v['proposal_id']}: {v['why']}")
    if llm_cache is not None:
        used = Counter(r.source for r in result.messages.values())
        why = Counter(e.split(" '")[0] for r in result.messages.values() for e in r.errors)
        print(f"guest messages: {dict(used)}; template because: {dict(why)}")
    print(f"wrote {n} decisions to {out}")
    if trace_path is not None:
        trace.write(result, trace_path)
        print(f"wrote trace to {trace_path}; see: plato40 explain {trace_path} [episode]")
    return 0


def _arbiter(
    data_dir: Path,
    paths: list[Path],
    out: Path,
    verdicts_path: Path,
    replace: list[str],
    llm_cache: Path | None,
) -> int:
    ds = load_dataset(data_dir)
    settings = Settings(data_root=data_dir, out_dir=out.parent, llm_cache=llm_cache)
    base = pipeline.run(ds, settings).decisions
    rows = [r for p in paths for r in bus.read(p)]
    decisions, verdicts = arbiter.arbitrate(ds, settings, base, rows, replace)
    n = contract.write_decisions(decisions, out)
    verdicts_path.write_text("", encoding="utf-8")
    bus.append(verdicts_path, verdicts)
    for v in verdicts:
        if not v["accepted"]:
            print(f"rejected {v['proposal_id']}: {v['why']}")
    ok = sum(v["accepted"] for v in verdicts)
    print(f"{ok} of {len(verdicts)} proposals accepted; replaced lanes: {replace or 'none'}")
    print(f"wrote {n} decisions to {out} and {len(verdicts)} verdicts to {verdicts_path}")
    return 0


def _validate(path: Path, data_dir: Path) -> int:
    ds = load_dataset(data_dir)
    decisions = contract.read_decisions(path)
    errors = pipeline.check(ds, RoomStates(ds), decisions)
    for e in errors:
        print(e, file=sys.stderr)
    print(f"{len(decisions)} decisions, {len(errors)} violations")
    return 1 if errors else 0


def _bus(data_dir: Path, lane: str, out: Path, cache: Path) -> int:
    ds = load_dataset(data_dir)
    settings = Settings(data_root=data_dir, out_dir=out)
    call = None
    if os.environ.get("PLATO40_LLM", "off") == "on":
        model = os.environ.get("PLATO40_LLM_MODEL", llm_guard.DEFAULT_MODEL)
        call = llm_guard.anthropic_call(model)
    rows, sources = lanes.registry()[lane](ds, settings, cache, call)
    decisions, verdicts = arbiter.arbitrate(ds, settings, [], rows)
    board = sorted(
        rows + verdicts,
        key=lambda r: (pd.Timestamp(r.get("t_known") or r["t"]), ROW_ORDER[r["row"]], r["id"]),
    )
    out.mkdir(parents=True, exist_ok=True)
    bus_path, dec_path = out / f"{lane}.bus.jsonl", out / f"{lane}.decisions.jsonl"
    bus_path.unlink(missing_ok=True)  # a new run starts a new board; rows are only appended
    bus.append(bus_path, board)
    contract.write_decisions(decisions, dec_path)
    req = ds.table("guest_requests.csv")
    asked = {
        str(k): pd.Timestamp(v)
        for k, v in zip(req["request_id"], req["timestamp_utc"], strict=True)
    }
    written: list[Mapping[str, Any]] = list(board)
    errors = bus.check_file(written, contract.known_asset_ids(ds.building))
    errors += bus.check_known_at(written, asked)
    errors += pipeline.check(ds, RoomStates(ds), decisions)
    n = {k: sum(r["row"] == k for r in board) for k in bus.ROWS}
    rejected = sum(not v["accepted"] for v in verdicts)
    print(f"{lane}: {n['signal']} signals, {n['proposal']} proposals, {rejected} rejected")
    print(f"{lane}: guest messages by source {dict(sorted(sources.items()))}")
    print(f"wrote {bus_path} and {len(decisions)} decisions to {dec_path}")
    for e in errors:
        print(e, file=sys.stderr)
    print(f"checks: bus contract, known-at, decision contract and policy: {len(errors)} violations")
    return 1 if errors else 0


def _audit(data_root: Path, out: Path) -> int:
    paths = discover_datasets(data_root)
    if not paths:
        print(f"no dataset (manifest.json) found under {data_root}", file=sys.stderr)
        return 2
    settings = Settings(data_root=data_root, out_dir=out)
    datasets = [load_dataset(p) for p in paths]
    from plato40.audit import report  # matplotlib is loaded only for the audit

    report.write(datasets, settings)
    print(f"wrote {settings.out_dir / report.REPORT_NAME} for {len(datasets)} dataset(s)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    args = _parser().parse_args(argv)
    if args.command == "run":
        return _run(
            args.data_dir,
            args.out,
            args.trace,
            args.llm_cache,
            tuple(lanes.registry()) if args.lanes is None else tuple(args.lanes),
            not args.no_follow_ups,
        )
    if args.command == "arbiter":
        return _arbiter(
            args.data_dir, args.bus, args.out, args.verdicts, args.replace, args.llm_cache
        )
    if args.command == "explain":
        print(trace.explain(args.trace, args.episode))
        return 0
    if args.command == "validate":
        return _validate(args.decisions, args.data_dir)
    if args.command == "ui":
        n = ui.render(args.decisions, args.trace, args.out)
        print(f"wrote {args.out} with {n} episodes")
        return 0
    if args.command == "bus":
        return _bus(args.data_dir, args.lane, args.out, args.llm_cache)
    return _audit(args.data_root, args.out)
