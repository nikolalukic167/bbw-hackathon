"""End-to-end check on real data; runs only when PLATO40_QUICKSTART points at it."""

import hashlib
import os
from pathlib import Path

import pytest

from plato40.audit import report
from plato40.load.reader import load_dataset
from plato40.settings import Settings

QUICKSTART = os.environ.get("PLATO40_QUICKSTART") or None


def _digest(directory: Path) -> dict[str, str]:
    return {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


@pytest.mark.skipif(QUICKSTART is None, reason="set PLATO40_QUICKSTART to a dataset directory")
def test_audit_is_byte_identical_across_runs(tmp_path: Path) -> None:
    assert QUICKSTART is not None
    data = Path(QUICKSTART)
    outputs = []
    for run in ("a", "b"):
        settings = Settings(data_root=data, out_dir=tmp_path / run)
        report.write([load_dataset(data)], settings)
        outputs.append(_digest(tmp_path / run))
    assert outputs[0] == outputs[1]
    assert "01_data_audit.md" in outputs[0]
