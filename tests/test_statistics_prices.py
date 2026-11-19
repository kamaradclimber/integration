"""Prices from the long-term statistics of a price sensor (pre-setup)."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.tarifwacht.lookback import FallbackPrice
from custom_components.tarifwacht.prices import PriceTable
from custom_components.tarifwacht.statistics_prices import (
    StatisticsPriceSource,
    unit_factor,
)
from custom_components.tarifwacht.timeslots import QUARTER


def hour(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(UTC)


def row(hour_ts: str, mean: float | None) -> dict:
    return {"start": hour(hour_ts).timestamp(), "mean": mean}


def test_hour_is_spread_over_its_quarters() -> None:
    source = StatisticsPriceSource.from_statistics(
        [row("2024-03-01T07:00:00", 0.16)], 1.0
    )
    base = hour("2024-03-01T07:00:00")
    assert [source.get(base + i * QUARTER) for i in range(4)] == [0.16] * 4
    assert source.get(base + 4 * QUARTER) is None
    assert source.get(base - QUARTER) is None


def test_hours_are_read_as_utc_regardless_of_input_offset() -> None:
    source = StatisticsPriceSource.from_statistics(
        [row("2024-03-01T07:00:00+01:00", 0.16)], 1.0
    )
    assert source.get(datetime(2024, 3, 1, 6, 0, tzinfo=UTC)) == 0.16


def test_rows_without_a_mean_are_skipped() -> None:
    source = StatisticsPriceSource.from_statistics(
        [row("2024-03-01T07:00:00", None), row("2024-03-01T08:00:00", 0.2)],
        1.0,
    )
    assert len(source) == 4
    assert source.get(hour("2024-03-01T07:00:00")) is None
    assert source.get(hour("2024-03-01T08:00:00")) == 0.2


def test_unit_factor() -> None:
    assert unit_factor("€/kWh") == 1.0
    assert unit_factor("EUR/kWh") == 1.0
    assert unit_factor("c€/kWh") == 0.01
    assert unit_factor("ct/kWh") == 0.01
    assert unit_factor("€/MWh") == 0.001
    assert unit_factor("€/kWh ") == 1.0  # padding tolerated
    assert unit_factor("USD/kWh") is None
    assert unit_factor(None) is None


def test_unit_factor_applies() -> None:
    source = StatisticsPriceSource.from_statistics(
        [row("2024-03-01T07:00:00", 16.54)], unit_factor("c€/kWh")
    )
    assert abs(source.get(hour("2024-03-01T07:00:00")) - 0.1654) < 1e-12


def test_live_table_wins_over_statistics() -> None:
    live = PriceTable()
    live.add(hour("2024-03-01T07:15:00"), 0.31)
    stats = StatisticsPriceSource.from_statistics(
        [row("2024-03-01T07:00:00", 0.16)], 1.0
    )
    price = FallbackPrice(live, stats, until=hour("2024-03-02T00:00:00"))
    assert price.get(hour("2024-03-01T07:15:00")) == 0.31
    assert price.get(hour("2024-03-01T07:00:00")) == 0.16
    assert price.get(hour("2024-03-03T00:00:00")) is None  # after until
