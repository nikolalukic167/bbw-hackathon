"""Command-line entry point.

``plato40 run <data_dir> --out decisions.jsonl``   write decisions for one dataset
``plato40 validate <decisions.jsonl> <data_dir>``  check contract and autonomy policy
``plato40 audit <data_root> --out <dir>``          write the data audit report
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from plato40 import contract, pipeline
from plato40.audit import report
from plato40.load.reader import discover_datasets, load_dataset
from plato40.settings import Settings
from plato40.state import RoomStates


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plato40", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="write decisions.jsonl for one dataset")
    run.add_argument("data_dir", type=Path, help="one dataset directory (with manifest.json)")
    run.add_argument("--out", type=Path, required=True, help="decisions.jsonl to write")
    val = sub.add_parser("validate", help="check a decisions file against contract and policy")
    val.add_argument("decisions", type=Path, help="decisions.jsonl to check")
    val.add_argument("data_dir", type=Path, help="the dataset the decisions were made on")
    audit = sub.add_parser("audit", help="write the data audit report")
    audit.add_argument(
        "data_root",
        type=Path,
        help="a dataset directory, or a directory containing several (quickstart, dev)",
    )
    audit.add_argument("--out", type=Path, required=True, help="report output directory")
    return parser


def _run(data_dir: Path, out: Path) -> int:
    ds = load_dataset(data_dir)
    decisions = pipeline.run(ds, Settings(data_root=data_dir, out_dir=out.parent))
    n = contract.write_decisions(decisions, out)
    print(f"wrote {n} decisions to {out}")
    return 0


def _validate(path: Path, data_dir: Path) -> int:
    ds = load_dataset(data_dir)
    decisions = contract.read_decisions(path)
    errors = pipeline.check(ds, RoomStates(ds), decisions)
    for e in errors:
        print(e, file=sys.stderr)
    print(f"{len(decisions)} decisions, {len(errors)} violations")
    return 1 if errors else 0


def _audit(data_root: Path, out: Path) -> int:
    paths = discover_datasets(data_root)
    if not paths:
        print(f"no dataset (manifest.json) found under {data_root}", file=sys.stderr)
        return 2
    settings = Settings(data_root=data_root, out_dir=out)
    datasets = [load_dataset(p) for p in paths]
    report.write(datasets, settings)
    print(f"wrote {settings.out_dir / report.REPORT_NAME} for {len(datasets)} dataset(s)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    args = _parser().parse_args(argv)
    if args.command == "run":
        return _run(args.data_dir, args.out)
    if args.command == "validate":
        return _validate(args.decisions, args.data_dir)
    return _audit(args.data_root, args.out)
