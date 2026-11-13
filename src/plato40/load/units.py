"""Units and physical plausibility bounds for every numeric series.

Bounds are our assumptions, chosen from building.json ratings where they exist and
from physics otherwise. A value outside its bounds is flagged as implausible; it is
never silently clipped or dropped by the loader.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bound:
    """Plausible closed range for one series.

    Attributes:
        unit: Unit of the stored value.
        low: Lowest plausible value, inclusive.
        high: Highest plausible value, inclusive.
        basis: Why these bounds were chosen.

    """

    unit: str
    low: float
    high: float
    basis: str


BOUNDS: dict[tuple[str, str], Bound] = {
    ("room_telemetry.csv", "temperature_c"): Bound(
        "degC", 5.0, 40.0, "heated indoor room; frost protection to overheating"
    ),
    ("room_telemetry.csv", "setpoint_c"): Bound(
        "degC", 15.0, 26.0, "18 degC setback, guests adjust 19-24 degC"
    ),
    ("room_telemetry.csv", "humidity_pct"): Bound("%", 5.0, 95.0, "relative humidity indoors"),
    ("room_telemetry.csv", "co2_ppm"): Bound("ppm", 350.0, 5000.0, "outdoor floor to IDLH-safe"),
    ("room_telemetry.csv", "presence"): Bound("0/1", 0.0, 1.0, "binary flag"),
    ("room_telemetry.csv", "window_open"): Bound("0/1", 0.0, 1.0, "binary flag"),
    ("energy.csv", "building_load_kw"): Bound("kW", 0.0, 200.0, "40-room hotel, 95 kW HP"),
    ("energy.csv", "hvac_electric_kw"): Bound("kW", 0.0, 70.0, "95 kW thermal at COP >= 1.4"),
    ("energy.csv", "pv_kw"): Bound("kW", 0.0, 60.0, "PV1 peak 60 kWp"),
    ("energy.csv", "battery_discharge_kw_signed"): Bound("kW", -40.0, 40.0, "BAT1 +-40 kW"),
    ("energy.csv", "battery_soc_kwh"): Bound("kWh", 0.0, 100.0, "BAT1 usable 100 kWh"),
    ("energy.csv", "grid_import_kw"): Bound("kW", 0.0, 250.0, "non-negative import"),
    ("energy.csv", "grid_export_kw"): Bound("kW", 0.0, 100.0, "non-negative export"),
    ("weather.csv", "outdoor_c"): Bound("degC", -30.0, 42.0, "Vienna climate extremes"),
    ("weather.csv", "cloud_cover_pct"): Bound("%", 0.0, 100.0, "percentage"),
    ("weather_forecast.csv", "outdoor_c_forecast"): Bound("degC", -30.0, 42.0, "as measured"),
    ("tariff.csv", "price_eur_kwh"): Bound("EUR/kWh", -0.5, 2.0, "day-ahead incl. grid fees"),
    ("equipment.csv", "HP1/heat_output_kw"): Bound("kW", 0.0, 120.0, "95 kW nominal at 7 degC"),
    ("equipment.csv", "HP1/electric_kw"): Bound("kW", 0.0, 70.0, "as hvac_electric_kw"),
    ("equipment.csv", "HP1/supply_temp_c"): Bound("degC", 15.0, 75.0, "heating and DHW flow"),
    ("equipment.csv", "AHU1/airflow_m3h"): Bound("m3/h", 0.0, 5500.0, "4200 m3/h nominal"),
    ("equipment.csv", "AHU1/filter_dp_pa"): Bound("Pa", 0.0, 600.0, "typical final dp 250-450"),
    ("equipment.csv", "AHU1/fan_kw"): Bound("kW", 0.0, 10.0, "small AHU fans"),
    ("equipment.csv", "AHU1/supply_air_c"): Bound("degC", 5.0, 40.0, "tempered supply air"),
    ("logistics.csv", "fill_pct"): Bound("%", 0.0, 100.0, "capped at 100 by the source"),
    ("logistics.csv", "stock_drinks"): Bound("units", 0.0, 100.0, "VEND1 capacity 100"),
    ("logistics.csv", "stock_snacks"): Bound("units", 0.0, 60.0, "VEND1 capacity 60"),
}

ROOM_WATER_DELTA = Bound("L/15min", 0.0, 300.0, "one room: tap 12 L/min, bath fill, 15 min")
MAIN_WATER_DELTA = Bound("L/15min", 0.0, 3000.0, "40 rooms plus common areas")

HP1_STATUS_DOCUMENTED = ("running", "standby", "defrost")
HOUSEKEEPING_STATES = ("clean", "dirty", "in_progress", "inspection_required")
