"""Markdown and figure helpers for generated audit reports.

Figures are written as PNG with fixed size, DPI and no software/date metadata, so a
rerun with the same library versions produces identical bytes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import matplotlib as mpl

mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

PNG_METADATA = {"Software": None}

plt.rcParams.update(
    {
        "figure.figsize": (9.0, 3.6),
        "axes.grid": True,
        "grid.alpha": 0.3,
        "font.size": 9,
        "axes.spines.top": False,
        "axes.spines.right": False,
    }
)


def save_figure(fig: Figure, figures_dir: Path, name: str, dpi: int) -> str:
    """Save ``fig`` as ``figures_dir/name.png`` and return the report-relative path."""
    figures_dir.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(figures_dir / f"{name}.png", dpi=dpi, metadata=PNG_METADATA)
    plt.close(fig)
    return f"figures/{name}.png"


def new_figure(rows: int = 1, height: float = 3.6) -> tuple[Figure, Any]:
    """Return a figure with ``rows`` vertically stacked axes sharing the x axis."""
    fig, axes = plt.subplots(rows, 1, figsize=(9.0, height * rows), sharex=True, squeeze=False)
    return fig, axes[:, 0]


def fmt(value: Any, digits: int = 1) -> str:
    """Format a number for prose: thousands separator, fixed decimals, '-' if missing."""
    if value is None:
        return "-"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f != f:
        return "-"
    if digits == 0 or (f.is_integer() and abs(f) >= 1000):
        return f"{f:,.0f}"
    return f"{f:,.{digits}f}"


def table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Return a GitHub-flavoured markdown table."""
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        cells = [str(c).replace("|", "\\|").replace("\n", " ") for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def image(path: str, caption: str) -> str:
    """Return a markdown image with an italic caption line below it."""
    return f"![{caption}]({path})\n\n*{caption}*"


def iso(ts: Any) -> str:
    """Format a timestamp as minute-precision ISO 8601 with its UTC offset or Z."""
    if ts is None or ts != ts:
        return "-"
    text = str(ts.isoformat(timespec="minutes"))
    return text.replace("+00:00", "Z")
