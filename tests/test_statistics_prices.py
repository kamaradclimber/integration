"""Prices from the long-term statistics of a price sensor (pre-setup)."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.tarifwacht.lookback import FallbackPrice
from custom_components.tarifwacht.prices import PriceTable
from custom_components.tarifwacht.statistics_prices import StatisticsPriceSource
from custom_components.tarifwacht.timeslots import QUARTER


def hour(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(UTC)


def row(hour_ts: str, mean: float | None) -> dict:
    return {"start": hour(hour_ts).timestamp(), "mean": mean}


def test_hour_is_spread_over_its_quarters() -> None:
    source = StatisticsPriceSource.from_statistics([row("2024-03-01T07:00:00", 0.16)])
    base = hour("2024-03-01T07:00:00")
    assert [source.get(base + i * QUARTER) for i in range(4)] == [0.16] * 4
    assert source.get(base + 4 * QUARTER) is None
    assert source.get(base - QUARTER) is None


def test_hours_are_read_as_utc_regardless_of_input_offset() -> None:
    source = StatisticsPriceSource.from_statistics(
        [row("2024-03-01T07:00:00+01:00", 0.16)]
    )
    assert source.get(datetime(2024, 3, 1, 6, 0, tzinfo=UTC)) == 0.16


def test_rows_without_a_mean_are_skipped() -> None:
    source = StatisticsPriceSource.from_statistics(
        [row("2024-03-01T07:00:00", None), row("2024-03-01T08:00:00", 0.2)]
    )
    assert len(source) == 4
    assert source.get(hour("2024-03-01T07:00:00")) is None
    assert source.get(hour("2024-03-01T08:00:00")) == 0.2


def test_zero_is_a_price() -> None:
    source = StatisticsPriceSource.from_statistics([row("2024-03-01T07:00:00", 0.0)])
    assert source.get(hour("2024-03-01T07:00:00")) == 0.0


def test_live_table_wins_over_statistics() -> None:
    live = PriceTable()
    live.add(hour("2024-03-01T07:15:00"), 0.31)
    stats = StatisticsPriceSource.from_statistics([row("2024-03-01T07:00:00", 0.16)])
    price = FallbackPrice(live, stats, until=hour("2024-03-02T00:00:00"))
    assert price.get(hour("2024-03-01T07:15:00")) == 0.31
    assert price.get(hour("2024-03-01T07:00:00")) == 0.16
    assert price.get(hour("2024-03-03T00:00:00")) is None  # after until
