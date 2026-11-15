"""The simulate_battery action (made-up values)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.tarifwacht.const import DOMAIN
from custom_components.tarifwacht.lookback import Lookback
from custom_components.tarifwacht.runtime import MeterRuntime

from .conftest import METER, mock_entry
from .test_solar import CHARGED, DISCHARGED, FEED_IN, PV, set_sources, solar_entry

HOUR = timedelta(hours=1)
NOON = datetime(2026, 6, 1, 10, tzinfo=UTC)


def hourly() -> dict:
    """A June day: 3 kWh PV at noon (1 kWh used, 2 kWh into the real
    battery), 2 kWh in the evening (1.8 from the battery, 0.2 bought)."""
    return {
        METER: {NOON: 0.0, NOON + 8 * HOUR: 0.2},
        FEED_IN: {NOON: 0.0, NOON + 8 * HOUR: 0.0},
        PV: {NOON: 3.0, NOON + 8 * HOUR: 0.0},
        CHARGED: {NOON: 2.0},
        DISCHARGED: {NOON + 8 * HOUR: 1.8},
    }


async def call(hass: HomeAssistant, entry_id: str, **data: Any) -> dict:
    async def fake_hourly(self: MeterRuntime, *_: Any) -> dict:
        return hourly()

    async def fake_lookback(self: MeterRuntime, start: datetime) -> Lookback:
        return Lookback(
            consumption={datetime(2026, 5, 1, tzinfo=UTC): 0.0},
            tariffs=self.engine.tariffs,
            until=self.engine.started,
        )

    with (
        patch.object(MeterRuntime, "_async_hourly", fake_hourly),
        patch.object(MeterRuntime, "async_lookback", fake_lookback),
    ):
        return await hass.services.async_call(
            DOMAIN,
            "simulate_battery",
            {"config_entry": entry_id, **data},
            blocking=True,
            return_response=True,
        )


async def test_simulation_per_size(hass: HomeAssistant) -> None:
    """Without the real battery: 2 kWh surplus at noon, 2 kWh needed in the
    evening. A lossless 2 kWh battery takes it all (60 ct no longer bought,
    16 ct no longer fed in); 1 kWh moves half."""
    set_sources(hass)
    entry = solar_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    response = await call(
        hass,
        entry.entry_id,
        start="2026-06-01 00:00:00",
        end="2026-06-02 00:00:00",
        sizes=["2", "1"],
        power=10,
        efficiency=100,
    )
    assert response["rough"] is True
    assert response["assumptions"] == {
        "efficiency_percent": 100.0,
        "grid_charging": False,
    }
    rows = {row["tariff"]: row for row in response["tariffs"]}
    fixed = rows["Fixed price"]
    assert [s["size_kwh"] for s in fixed["sizes"]] == [1.0, 2.0]
    assert fixed["sizes"][0]["saving"] == 0.22
    assert fixed["sizes"][1]["saving"] == 0.44
    assert fixed["sizes"][1]["full_cycles"] == 1.0
    assert fixed["sizes"][1]["power_kw"] == 10.0
    # The real battery beside it: as in the compare action.
    assert fixed["your_battery_saving"] == 0.38
    assert [m["month"] for m in response["months"]] == ["2026-06"]


async def test_defaults(hass: HomeAssistant) -> None:
    """Without sizes 5, 10 and 15 kWh at half their size per hour."""
    set_sources(hass)
    entry = solar_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    response = await call(
        hass, entry.entry_id, start="2026-06-01 00:00:00", end="2026-06-02 00:00:00"
    )
    sizes = response["tariffs"][0]["sizes"]
    assert [(s["size_kwh"], s["power_kw"]) for s in sizes] == [
        (5.0, 2.5),
        (10.0, 5.0),
        (15.0, 7.5),
    ]
    assert response["assumptions"]["efficiency_percent"] == 85.0


async def test_without_pv_and_battery(hass: HomeAssistant) -> None:
    set_sources(hass)
    entry = mock_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, entry.entry_id)
    assert err.value.translation_key == "simulation_no_solar"


async def test_sizes_are_checked(hass: HomeAssistant) -> None:
    set_sources(hass)
    entry = solar_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(Exception, match="kWh"):
        await call(hass, entry.entry_id, sizes=["viel"])
