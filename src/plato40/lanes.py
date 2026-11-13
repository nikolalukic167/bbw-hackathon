"""Registry of the bus trigger lanes that ``plato40 run`` runs (docs/BUS.md).

A lane is a module with ``rows(ds, settings, cache, call)`` returning its signal and
proposal rows in time order and the count of guest messages by source. To add a lane,
add its enums to ``bus.LANES``, its rank to ``arbiter.LANE_RANK`` and one line here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from plato40.llm_guard import Call
from plato40.load.reader import Dataset
from plato40.settings import Settings

Rows = Callable[[Dataset, Settings, Path, Call | None], tuple[list[dict[str, Any]], Counter[str]]]


def registry() -> dict[str, Rows]:
    """Return every lane by name, in the order they run."""
    from plato40 import lane_s03, lane_s05, lane_s06

    return {"s03": lane_s03.rows, "s05": lane_s05.rows, "s06": lane_s06.rows}
