"""Time-of-availability rules (ADR-002).

A row is usable by a decision at time ``t`` only if it was known at ``t``. For files
with an explicit availability column (tariff ``published_utc``, forecast
``issued_utc``, roster ``status_known_utc``, booking ``created_utc``, work order
``created_utc``) that column decides. For measured series the row is assumed known at
its own timestamp: the README says a row stamped ``t`` describes the state at ``t``,
and no transmission delay is documented.
"""

from __future__ import annotations

import pandas as pd

from plato40.load.schema import FileSchema


def known_at(frame: pd.DataFrame, schema: FileSchema) -> pd.Series:
    """Return the UTC time at which each row became known."""
    column = schema.known_at_column or schema.time_column
    return frame[column]


def lead_time_hours(frame: pd.DataFrame, schema: FileSchema) -> pd.Series:
    """Return effective time minus known time, in hours, per row.

    Positive values mean the information arrived before the time it describes;
    negative values mean it arrived late.
    """
    delta = frame[schema.time_column] - known_at(frame, schema)
    return delta.dt.total_seconds() / 3600.0
