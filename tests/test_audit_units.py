import numpy as np
import pandas as pd

from plato40.audit.candidates import _runs_of_days, theil_sen
from plato40.audit.derive import meter_deltas
from plato40.audit.render import fmt, table


def test_meter_reset_is_missing_and_recorded() -> None:
    idx = pd.date_range("2026-01-01", periods=4, freq="15min", tz="UTC")
    wide = pd.DataFrame({"R101": [100.0, 101.0, 0.5, 1.5]}, index=idx)
    out = meter_deltas(wide, reset_drop_l=50.0)
    assert np.isnan(out.deltas["R101"].iloc[2])
    assert out.deltas["R101"].iloc[3] == 1.0
    assert out.resets["drop_l"].tolist() == [100.5]


def test_small_negative_step_is_kept_as_jitter() -> None:
    idx = pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC")
    wide = pd.DataFrame({"R101": [10.0, 9.0, 9.5]}, index=idx)
    out = meter_deltas(wide, reset_drop_l=50.0)
    assert out.deltas["R101"].iloc[1] == -1.0
    assert out.resets.empty


def test_theil_sen_ignores_a_minority_of_outliers() -> None:
    x = np.arange(20, dtype=float)
    y = 2.0 + 0.5 * x
    y[[3, 9, 15]] = -10.0
    slope, intercept = theil_sen(x, y)
    assert abs(slope - 0.5) < 1e-9
    assert abs(intercept - 2.0) < 1e-9


def test_runs_of_days_groups_consecutive_dates() -> None:
    days = [pd.Timestamp(d).date() for d in ("2026-01-01", "2026-01-02", "2026-01-04")]
    runs = _runs_of_days(list(days))
    assert [r[2] for r in runs] == [2, 1]


def test_fmt_and_table() -> None:
    assert fmt(float("nan")) == "-"
    assert fmt(12345.0, 0) == "12,345"
    assert table(["a"], [["x|y"]]).splitlines()[-1] == "| x\\|y |"
