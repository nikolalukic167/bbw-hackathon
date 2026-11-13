"""Command-line entry point: ``plato40 audit <data_root> --out <dir>``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from plato40.audit import report
from plato40.load.reader import discover_datasets, load_dataset
from plato40.settings import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plato40", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit", help="write the data audit report")
    audit.add_argument(
        "data_root",
        type=Path,
        help="a dataset directory, or a directory containing several (quickstart, dev)",
    )
    audit.add_argument("--out", type=Path, required=True, help="report output directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    args = _parser().parse_args(argv)
    if args.command == "audit":
        paths = discover_datasets(args.data_root)
        if not paths:
            print(f"no dataset (manifest.json) found under {args.data_root}", file=sys.stderr)
            return 2
        settings = Settings(data_root=args.data_root, out_dir=args.out)
        datasets = [load_dataset(p) for p in paths]
        report.write(datasets, settings)
        print(f"wrote {settings.out_dir / report.REPORT_NAME} for {len(datasets)} dataset(s)")
    return 0
