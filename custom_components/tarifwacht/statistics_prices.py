"""Prices from the long-term statistics of a price sensor.

For the time before Tarifwacht read a total-price sensor (or where its
recorder states are gone), the hourly means of the sensor's long-term
statistics are used: the price of an hour applies to its four quarter
hours. Only the sensor's own statistics are read (local, no request); a
sensor without statistics or with an unknown unit yields no prices and
nothing changes. The values are not written back anywhere (the engine's
price table is not persisted with them); they are read again at each start.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .timeslots import QUARTER

# Unit of measurement of the sensor -> factor to EUR/kWh. Statistics are
# stored in the sensor's unit; only these units are converted, anything
# else is rejected (a wrong guess would silently book wrong prices).
_UNIT_FACTORS = {
    "€/kWh": 1.0,
    "EUR/kWh": 1.0,
    "c€/kWh": 0.01,
    "ct/kWh": 0.01,
    "€/MWh": 0.001,
}


def unit_factor(unit: str | None) -> float | None:
    """The factor that turns the sensor's unit into EUR/kWh, None if unknown."""
    if not unit:
        return None
    return _UNIT_FACTORS.get("".join(str(unit).split()))


class StatisticsPriceSource:
    """Prices of a price sensor read from its long-term statistics.

    Hours without a mean are left out (never a price of zero). The mean of
    an hour is used for each of its four quarter hours.
    """

    def __init__(self, prices: dict[datetime, float]) -> None:
        self.prices = prices

    @classmethod
    def from_statistics(
        cls, rows: Iterable[Any], factor: float
    ) -> StatisticsPriceSource:
        prices: dict[datetime, float] = {}
        for row in rows:
            mean = row.get("mean")
            start = row.get("start")
            if mean is None or start is None:
                continue
            hour = datetime.fromtimestamp(start, tz=UTC).replace(
                minute=0, second=0, microsecond=0
            )
            price = float(mean) * factor
            for i in range(4):
                prices[hour + i * QUARTER] = price
        return cls(prices)

    def get(self, slot: datetime) -> float | None:
        return self.prices.get(slot)

    def __len__(self) -> int:
        return len(self.prices)
