"""A total-price sensor's history read from its long-term statistics.

Backfilled price statistics (as written by hand or by another tool) fill
the time before Tarifwacht read the sensor: the compare action and the
import of statistics book a dynamic total-price tariff through them,
without SMARD.de.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.models.statistics import StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from .conftest import PRICE
from .test_init import START
from .test_lookback import compare, import_meter_statistics, setup_entry
from .test_statistics_import import (
    LIVE_HOUR,
    entity_id,
    import_statistics,
    live_statistics,
)

FIRST_PRICE_HOUR = datetime(2026, 10, 24, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Recorder, enable_custom_integrations: None
) -> None:
    """The recorder must be set up before `hass` (listed first)."""


async def import_price_statistics(hass: HomeAssistant, unit: str = "€/kWh") -> None:
    """Backfilled hourly prices of the day before the setup (€/kWh), written
    after Tarifwacht was set up, as by hand or by another tool.

    Hours 12, 13, 14 and 15 UTC: 20, 20, 25 and 30 ct. The meter statistics
    (import_meter_statistics) have energy for 13, 14 and 15 UTC.
    """
    metadata: dict[str, Any] = {
        "has_sum": False,
        "mean_type": StatisticMeanType.ARITHMETIC,
        "name": None,
        "source": "recorder",
        "statistic_id": PRICE,
        "unit_of_measurement": unit,
    }
    if "unit_class" in StatisticMetaData.__annotations__:
        metadata["unit_class"] = None
    prices = [0.20, 0.20, 0.25, 0.30]
    async_import_statistics(
        hass,
        metadata,  # type: ignore[arg-type]
        [
            {"start": FIRST_PRICE_HOUR + timedelta(hours=i), "mean": price}
            for i, price in enumerate(prices)
        ],
    )
    await async_wait_recording_done(hass)


@pytest.mark.freeze_time(START)
async def test_compare_prices_from_statistics(hass: HomeAssistant) -> None:
    """The time before the setup books from the sensor's statistics:
    13 UTC 1 kWh at 20 ct, 14 UTC 1 kWh at 25 ct, 15 UTC 2 kWh at 30 ct."""
    entry = await setup_entry(hass, market=False)
    await import_meter_statistics(hass)
    # Written after the setup: read anew, not cached from the start.
    await import_price_statistics(hass)

    result = await compare(hass, entry, start="2026-10-24 15:00:00")
    assert result["energy_kwh"] == pytest.approx(4.0)
    tariffs = {t["tariff"]: t for t in result["tariffs"]}
    dynamic = tariffs["Dynamic"]
    assert dynamic["energy_without_price_kwh"] == pytest.approx(0.0)
    assert dynamic["consumption_cost"] == pytest.approx(1 * 0.20 + 1 * 0.25 + 2 * 0.30)
    assert dynamic["difference_to_current"] == pytest.approx(
        dynamic["consumption_cost"] - tariffs["Fixed price"]["cost"]
    )
    # All prices known: the dynamic tariff counts as cheapest.
    assert isinstance(result["cheapest"], str)


@pytest.mark.freeze_time(START)
async def test_compare_without_statistics_leaves_prices_out(
    hass: HomeAssistant,
) -> None:
    """Without backfilled statistics the total price sensor has no prices
    before the setup: the dynamic tariff is left out, nothing booked."""
    entry = await setup_entry(hass, market=False)
    await import_meter_statistics(hass)

    result = await compare(hass, entry, start="2026-10-24 15:00:00")
    assert result["energy_kwh"] == pytest.approx(4.0)
    tariffs = {t["tariff"]: t for t in result["tariffs"]}
    assert tariffs["Dynamic"]["energy_without_price_kwh"] == pytest.approx(4.0)
    assert tariffs["Dynamic"]["difference_to_current"] is None


@pytest.mark.freeze_time(START)
async def test_compare_ignores_statistics_in_another_unit(
    hass: HomeAssistant,
) -> None:
    """Hourly statistics recorded in another unit (ct/kWh) are not used as
    prices: the dynamic tariff is left out, like without statistics."""
    entry = await setup_entry(hass, market=False)
    await import_meter_statistics(hass)
    await import_price_statistics(hass, unit="ct/kWh")

    result = await compare(hass, entry, start="2026-10-24 15:00:00")
    assert result["energy_kwh"] == pytest.approx(4.0)
    tariffs = {t["tariff"]: t for t in result["tariffs"]}
    assert tariffs["Dynamic"]["energy_without_price_kwh"] == pytest.approx(4.0)
    assert tariffs["Dynamic"]["difference_to_current"] is None


@pytest.mark.freeze_time(START)
async def test_import_statistics_prices(hass: HomeAssistant) -> None:
    """Import statistics writes the time before the setup from the sensor's
    statistics: 3 hours with energy, 1.05 €."""
    entry = await setup_entry(hass, market=False)
    await import_meter_statistics(hass)
    await import_price_statistics(hass)
    dynamic_cost = entity_id(hass, entry, "dynamic_cost")
    await live_statistics(hass, {dynamic_cost: "EUR"})

    result = await import_statistics(hass, entry, "2026-10-24")
    await async_wait_recording_done(hass)
    assert result["start"].startswith("2026-10-24T14:00:00+02:00")
    assert result["skipped"] == []
    dynamic = result["sensors"][dynamic_cost]
    # 28 hours from 24.10. 12:00 UTC up to the sensor's first own row
    # (25.10. 16:00 UTC): the last written row starts 15:00 UTC (CET).
    assert dynamic["hours"] == 28
    assert dynamic["added"] == pytest.approx(1 * 0.20 + 1 * 0.25 + 2 * 0.30)
    assert dynamic["until"].startswith("2026-10-25T16:00:00+01:00")

    # Really written: the first three hours before the sensor's own first row.
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 10, 20, tzinfo=UTC),
        None,
        {dynamic_cost},
        "hour",
        None,
        {"sum"},
    )
    sums = {
        datetime.fromtimestamp(row["start"], UTC): row["sum"]
        for row in rows[dynamic_cost]
    }
    # The first hour with energy books 0.20, the running sum reaches 1.05
    # and shifts the sensor's own first row (live 0.5) by it.
    assert sums[datetime(2026, 10, 24, 13, tzinfo=UTC)] == pytest.approx(0.20)
    assert sums[datetime(2026, 10, 24, 15, tzinfo=UTC)] == pytest.approx(1.05)
    assert sums[LIVE_HOUR] == pytest.approx(0.5 + 1.05)
