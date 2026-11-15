"""Imagined battery (no Home Assistant). Made-up values only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.tarifwacht.battery_sim import (
    SimSettings,
    per_year,
    simulate,
)
from custom_components.tarifwacht.const import BERLIN
from custom_components.tarifwacht.solar import HourFlows, SolarSettings
from custom_components.tarifwacht.tariffs import FixedPrice, Tariff
from custom_components.tarifwacht.timeslots import QUARTER

HOUR = timedelta(hours=1)
NOON = datetime(2026, 6, 1, 10, tzinfo=UTC)  # 12:00 local
FIXED = Tariff(FixedPrice(0.30))
AC = SolarSettings(same_inverter=False, feed_in_rate=0.08)
# Lossless and fast enough that only the size limits: easy numbers.
IDEAL = SimSettings(size=5.0, power=10.0, efficiency=1.0)


class Prices:
    """Quarter-hour prices per hour from a dict, None elsewhere."""

    def __init__(self, per_hour: dict[datetime, float]) -> None:
        self.prices = {
            hour + i * QUARTER: value
            for hour, value in per_hour.items()
            for i in range(4)
        }

    def get(self, slot: datetime) -> float | None:
        return self.prices.get(slot)


def day_and_evening(surplus: float, evening: float) -> dict[datetime, HourFlows]:
    """Noon: PV surplus fed in (1 kWh used); evening: bought from the grid."""
    return {
        NOON: HourFlows(feed_in=surplus, pv=surplus + 1.0),
        NOON + 8 * HOUR: HourFlows(purchase=evening),
    }


def run(hours, sims, tariffs=None, settings=AC):
    return simulate(hours, tariffs or {"fix": FIXED}, settings, sims, BERLIN)


def test_battery_moves_surplus_to_the_evening() -> None:
    """3 kWh surplus, 2 kWh in the evening: the whole surplus goes in, so
    2 kWh are no longer bought (+60 ct) and 3 kWh no longer fed in (-24 ct).
    The kWh left over waits for the next evening."""
    result = run(day_and_evening(3.0, 2.0), [IDEAL])
    t = result.totals["fix"][5.0]
    assert t.saving == pytest.approx(0.36)
    assert t.discharged == pytest.approx(2.0)
    assert t.saving_grid == pytest.approx(0.0)


def test_size_limits_the_saving() -> None:
    sims = [
        SimSettings(size=1.0, power=10.0, efficiency=1.0),
        SimSettings(size=5.0, power=10.0, efficiency=1.0),
    ]
    result = run(day_and_evening(3.0, 2.0), sims)
    assert result.totals["fix"][1.0].saving == pytest.approx(0.22)
    assert result.totals["fix"][5.0].saving == pytest.approx(0.36)


def test_power_limits_charging_and_discharging() -> None:
    sim = SimSettings(size=5.0, power=0.5, efficiency=1.0)
    result = run(day_and_evening(3.0, 2.0), [sim])
    # 0.5 kWh in at noon, 0.5 kWh out in the evening.
    assert result.totals["fix"][5.0].discharged == pytest.approx(0.5)


def test_cycle_loss() -> None:
    """81 % round trip: 2 kWh stored come back as 2 x 0.81 = 1.62 kWh,
    covering 1.62 of the 2 kWh; the surplus goes in as far as it helps."""
    sim = SimSettings(size=5.0, power=10.0, efficiency=0.81)
    result = run(day_and_evening(3.0, 2.0), [sim])
    t = result.totals["fix"][5.0]
    # Charged 3 kWh AC (whole surplus), 2.7 stored, 2.43 out, 2 needed.
    assert t.discharged == pytest.approx(2.0)
    # Purchase 0 instead of 2 kWh (+0.60), feed-in 0 instead of 3 (-0.24).
    assert t.saving == pytest.approx(0.36)


def test_existing_battery_taken_out_first() -> None:
    """A real battery (own inverter) moved the energy already; the imagined
    one starts from PV without battery and saves the same."""
    hours = {
        NOON: HourFlows(pv=4.0, charged=3.0),
        NOON + 8 * HOUR: HourFlows(discharged=2.0, purchase=0.0),
    }
    result = run(hours, [IDEAL])
    assert result.totals["fix"][5.0].saving == pytest.approx(0.36)


def test_grid_charging_with_changing_prices() -> None:
    """No PV: 2 kWh in the evening at 40 ct, the night before at 10 ct."""
    night = NOON - 10 * HOUR
    evening = NOON + 8 * HOUR
    hours = {night: HourFlows(purchase=0.0), evening: HourFlows(purchase=2.0)}
    dynamic = Tariff(Prices({night: 0.10, evening: 0.40}))
    sim = SimSettings(size=5.0, power=10.0, efficiency=1.0, grid_charging=True)
    t = run(hours, [sim], {"dyn": dynamic}).totals["dyn"][5.0]
    assert t.grid_charged == pytest.approx(2.0)
    # 2 kWh bought at 10 instead of 40 ct.
    assert t.saving == pytest.approx(0.60)
    assert t.saving_grid == pytest.approx(0.60)


def test_grid_charging_pays_only_beyond_the_loss() -> None:
    """30 ct against 32 ct with 89 % round trip: 30 / 0.89 = 33.7 ct > 32."""
    night = NOON - 10 * HOUR
    evening = NOON + 8 * HOUR
    hours = {night: HourFlows(), evening: HourFlows(purchase=2.0)}
    dynamic = Tariff(Prices({night: 0.30, evening: 0.32}))
    sim = SimSettings(size=5.0, power=10.0, efficiency=0.89, grid_charging=True)
    t = run(hours, [sim], {"dyn": dynamic}).totals["dyn"][5.0]
    assert t.grid_charged == pytest.approx(0.0)
    assert t.saving == pytest.approx(0.0)


def test_grid_charging_respects_size_and_power() -> None:
    night = NOON - 10 * HOUR
    evening = NOON + 8 * HOUR
    hours = {night: HourFlows(), evening: HourFlows(purchase=4.0)}
    dynamic = Tariff(Prices({night: 0.10, evening: 0.40}))
    sim = SimSettings(
        size=3.0, power=10.0, efficiency=1.0, grid_charging=True, grid_power=2.0
    )
    t = run(hours, [sim], {"dyn": dynamic}).totals["dyn"][3.0]
    # One night hour, 2 kW from the grid: 2 kWh, below the 3 kWh size.
    assert t.grid_charged == pytest.approx(2.0)


def test_grid_charging_without_room_between() -> None:
    """The battery is full from the PV between the cheap and the dear hour:
    no room for grid energy."""
    night = NOON - 10 * HOUR
    evening = NOON + 8 * HOUR
    hours = {
        night: HourFlows(),
        NOON: HourFlows(feed_in=5.0, pv=6.0),
        evening: HourFlows(purchase=8.0),
    }
    dynamic = Tariff(Prices({night: 0.05, NOON: 0.30, evening: 0.40}))
    sim = SimSettings(size=5.0, power=10.0, efficiency=1.0, grid_charging=True)
    t = run(hours, [sim], {"dyn": dynamic}).totals["dyn"][5.0]
    assert t.grid_charged == pytest.approx(0.0)
    assert t.discharged == pytest.approx(5.0)


def test_grid_charging_only_within_a_day() -> None:
    """A cheap hour 30 hours before is too far back (missing hours between
    do not shorten the distance)."""
    early = NOON - 22 * HOUR
    evening = NOON + 8 * HOUR
    hours = {early: HourFlows(), evening: HourFlows(purchase=2.0)}
    dynamic = Tariff(Prices({early: 0.05, evening: 0.40}))
    sim = SimSettings(size=5.0, power=10.0, efficiency=1.0, grid_charging=True)
    t = run(hours, [sim], {"dyn": dynamic}).totals["dyn"][5.0]
    assert t.grid_charged == pytest.approx(0.0)


def test_hour_without_price_left_out() -> None:
    hours = day_and_evening(3.0, 2.0)
    partial = Tariff(Prices({NOON: 0.30}))
    t = run(hours, [IDEAL], {"p": partial}).totals["p"][5.0]
    assert t.hours == 1
    assert t.hours_left_out == 1


def test_months_and_days() -> None:
    result = run(day_and_evening(3.0, 2.0), [IDEAL])
    assert result.days == 0  # 9 hours
    assert result.months["2026-06"]["fix"][5.0].saving == pytest.approx(0.36)


def test_per_year() -> None:
    assert per_year(10.0, 6) is None
    assert per_year(10.0, 73) == pytest.approx(50.0)
