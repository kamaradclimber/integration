"""Prices from the long-term statistics of a price sensor.

For the time before Tarifwacht read a total-price sensor (or where its
recorder states are gone), the hourly means of the sensor's long-term
statistics are used: the price of an hour applies to its four quarter
hours. Only the sensor's own statistics are read (local, no request); a
sensor without statistics or with a unit other than a price unit yields
no prices and nothing changes. The values are not written back anywhere
(the engine's price table is not persisted with them); they are read
again on every look-back.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .timeslots import QUARTER


class StatisticsPriceSource:
    """Prices of a price sensor read from its long-term statistics.

    Hours without a mean are left out (never a price of zero). The mean of
    an hour is used for each of its four quarter hours.
    """

    def __init__(self, prices: dict[datetime, float]) -> None:
        self.prices = prices

    @classmethod
    def from_statistics(cls, rows: Iterable[Any]) -> StatisticsPriceSource:
        """Prices in EUR/kWh from the mean of each hour."""
        prices: dict[datetime, float] = {}
        for row in rows:
            mean = row.get("mean")
            start = row.get("start")
            if mean is None or start is None:
                continue
            hour = datetime.fromtimestamp(start, tz=UTC).replace(
                minute=0, second=0, microsecond=0
            )
            for i in range(4):
                prices[hour + i * QUARTER] = float(mean)
        return cls(prices)

    def get(self, slot: datetime) -> float | None:
        return self.prices.get(slot)

    def __len__(self) -> int:
        return len(self.prices)
