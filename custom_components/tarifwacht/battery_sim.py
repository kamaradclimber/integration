"""What a battery of a given size would have saved, under each tariff.

An imagined battery on top of the PV, hour by hour over the metered past
(simon42 PN 95728 and topic 94979 #24): it charges from the PV surplus and
covers the consumption the PV does not, limited by its size and power and
with a loss per cycle. Compared with "PV without battery" of solar.py, so
an existing battery is taken out first (solar.SolarSettings.pv_alone) and
its real saving can be set beside the simulated ones.

Optionally it also charges from the grid (only worth it with prices that
change): energy bought in a cheaper hour covers a later, dearer one when
the price difference beats the cycle loss. Hindsight knows the PV and the
consumption of the next hours, a real control does not, so this part is an
upper bound ("at best"). The pairing is greedy: each hour that still buys
energy takes it from the cheapest earlier hour within GRID_WINDOW where the
battery had room the whole time between and the charging power was left.

Within an hour PV and consumption are netted (hourly statistics), so short
gaps a battery would cover are missed: the saving comes out rather low, as
for the real battery in solar.py. Standby losses and ageing are left out.

Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from .solar import HourFlows, SolarSettings, _hour_prices, _paid_share
from .tariffs import PriceSource, Tariff

# Default sizes (kWh) when the action names none.
DEFAULT_SIZES = (5.0, 10.0, 15.0)
# Default power as a share of the size per hour (0.5 C); own assumption,
# home batteries are usually specified between 0.5 C and 1 C.
DEFAULT_C_RATE = 0.5
# Default round trip, AC to AC: battery 95.0 % on average (HTW Berlin,
# Stromspeicher-Inspektion 2026, p. 17) times charging and discharging at
# about 97 % each (HTW 2025: 92 to 98.2 % when charging). See
# docs/ENTWICKLUNG.md, section 2d.
DEFAULT_EFFICIENCY = 0.89
# How far back a grid charge may lie before the hour it covers.
GRID_WINDOW = 24
# Lifetimes for "pays off up to a purchase price of".
LIFETIMES = (10, 15)
# Below this many days the yearly figure is not given.
MIN_DAYS = 7
# Below this many days the yearly figure is marked rough.
YEAR_DAYS = 365
EPSILON = 1e-9


@dataclass(frozen=True)
class SimSettings:
    """The imagined battery."""

    size: float  # kWh usable
    power: float  # kW charging and discharging
    efficiency: float = DEFAULT_EFFICIENCY  # round trip, AC to AC
    grid_charging: bool = False
    grid_power: float | None = None  # kW from the grid; None: `power`

    @property
    def charge_eff(self) -> float:
        return math.sqrt(self.efficiency)

    @property
    def discharge_eff(self) -> float:
        return math.sqrt(self.efficiency)


@dataclass
class SimTotals:
    """One tariff and one size over a period (EUR, kWh)."""

    pv_only: float = 0.0  # cost without a battery (purchase - feed-in)
    simulated: float = 0.0  # cost with the imagined battery
    simulated_pv_only: float = 0.0  # the same without grid charging
    discharged: float = 0.0  # kWh out of the battery
    grid_charged: float = 0.0  # kWh bought for the battery
    hours: int = 0
    hours_left_out: int = 0

    @property
    def saving(self) -> float:
        return self.pv_only - self.simulated

    @property
    def saving_grid(self) -> float:
        """Part of the saving that grid charging adds (at best)."""
        return self.simulated_pv_only - self.simulated


@dataclass
class SimResult:
    """Totals per tariff and size, for the period and per local month."""

    totals: dict[str, dict[float, SimTotals]]
    months: dict[str, dict[str, dict[float, SimTotals]]] = field(default_factory=dict)
    days: int = 0  # from the first to the end of the last hour, rounded
    hours: int = 0  # hours with meter values


@dataclass
class _Hour:
    start: datetime
    consumption: float
    pv: float
    price: float | None
    rate: float | None


def _run(
    hours: Sequence[_Hour], sim: SimSettings, *, grid: bool
) -> tuple[list[float], list[float], list[float]]:
    """Purchase, feed-in and grid charge per hour with the battery."""
    n = len(hours)
    soc = [0.0] * n  # state of charge (kWh stored) at the end of each hour
    purchase = [0.0] * n
    feed_in = [0.0] * n
    charge_in = [0.0] * n  # AC energy into the battery (PV and grid)
    grid_in = [0.0] * n
    level = 0.0
    eta_c, eta_d = sim.charge_eff, sim.discharge_eff
    grid_power = sim.power if sim.grid_power is None else sim.grid_power
    for i, hour in enumerate(hours):
        surplus = max(0.0, hour.pv - hour.consumption)
        deficit = max(0.0, hour.consumption - hour.pv)
        into = min(surplus, sim.power, (sim.size - level) / eta_c)
        level += into * eta_c
        out = min(deficit, sim.power, level * eta_d)
        level -= out / eta_d
        charge_in[i] = into
        feed_in[i] = surplus - into
        missing = deficit - out
        if grid and missing > EPSILON and hour.price is not None:
            # Only as much as the discharge power left in this hour allows;
            # the battery was empty before, so the grid charge is all there is.
            wanted = min(missing, sim.power - out)
            out += _grid_charge(
                hours, i, wanted, sim, soc, charge_in, grid_in, grid_power
            )
        purchase[i] = deficit - out
        soc[i] = level
    return purchase, feed_in, grid_in


def _grid_charge(
    hours: Sequence[_Hour],
    i: int,
    wanted: float,
    sim: SimSettings,
    soc: list[float],
    charge_in: list[float],
    grid_in: list[float],
    grid_power: float,
) -> float:
    """Cover up to `wanted` kWh of hour `i` from cheaper earlier hours;
    returns the kWh covered. Raises the stored energy between."""
    eta_c, eta_d = sim.charge_eff, sim.discharge_eff
    price_i = hours[i].price
    covered = 0.0
    # By time, not by position: hours without meter values may be missing.
    first = i
    window = timedelta(hours=GRID_WINDOW)
    while first > 0 and hours[i].start - hours[first - 1].start <= window:
        first -= 1
    while wanted - covered > EPSILON:
        best = None
        for j in range(first, i):
            price_j = hours[j].price
            if price_j is None or price_j / sim.efficiency >= price_i - EPSILON:
                continue
            if min(sim.power - charge_in[j], grid_power - grid_in[j]) <= EPSILON:
                continue
            if best is None or price_j < hours[best].price:  # type: ignore[operator]
                best = j
        if best is None:
            break
        # Room: the stored energy from `best` to the hour before `i` may not
        # exceed the size.
        room = min((sim.size - soc[k] for k in range(best, i)), default=sim.size)
        power_left = min(sim.power - charge_in[best], grid_power - grid_in[best])
        stored = min(room, power_left * eta_c, (wanted - covered) / eta_d)
        if stored <= EPSILON:
            # This hour cannot give more; look further without it.
            first = best + 1
            continue
        bought = stored / eta_c
        charge_in[best] += bought
        grid_in[best] += bought
        for k in range(best, i):
            soc[k] += stored
        covered += stored * eta_d
    # Stored, then used in hour i: the level after hour i stays as it was.
    return covered


def simulate(
    hours: Mapping[datetime, HourFlows],
    tariffs: Mapping[str, Tariff],
    settings: SolarSettings,
    sizes: Sequence[SimSettings],
    tz: tzinfo,
    market: PriceSource | None = None,
) -> SimResult:
    """Imagined batteries of `sizes` per tariff over `hours` (UTC hour
    starts). An hour without the tariff's price (or the market price under
    § 51) is simulated but its cost left out, as in solar.solar_totals."""
    starts = sorted(hours)
    result = SimResult(
        totals={key: {s.size: SimTotals() for s in sizes} for key in tariffs}
    )
    result.hours = len(starts)
    if starts:
        span = starts[-1] + timedelta(hours=1) - starts[0]
        result.days = round(span.total_seconds() / 86400)
    for key, tariff in tariffs.items():
        series = []
        for start in starts:
            flows = hours[start]
            prices = _hour_prices(tariff, start)
            paid = _paid_share(settings, market, start)
            price = None if prices is None else sum(prices) / len(prices)
            rate = None if paid is None else settings.feed_in_rate * paid
            series.append(
                _Hour(
                    start=start,
                    consumption=settings.consumption(flows),
                    pv=settings.pv_alone(flows),
                    price=price,
                    rate=rate,
                )
            )
        for sim in sizes:
            plain = _run(series, sim, grid=False)
            with_grid = _run(series, sim, grid=True) if sim.grid_charging else plain
            _book(result, key, sim, series, plain, with_grid, tz)
    return result


def _book(
    result: SimResult,
    key: str,
    sim: SimSettings,
    series: Sequence[_Hour],
    plain: tuple[list[float], list[float], list[float]],
    with_grid: tuple[list[float], list[float], list[float]],
    tz: tzinfo,
) -> None:
    total = result.totals[key][sim.size]
    for i, hour in enumerate(series):
        month = hour.start.astimezone(tz).strftime("%Y-%m")
        per_month = result.months.setdefault(month, {}).setdefault(key, {})
        targets = [total, per_month.setdefault(sim.size, SimTotals())]
        if hour.price is None or hour.rate is None:
            for t in targets:
                t.hours_left_out += 1
            continue
        alone_purchase = max(0.0, hour.consumption - hour.pv)
        alone_feed = max(0.0, hour.pv - hour.consumption)
        pv_only = alone_purchase * hour.price - alone_feed * hour.rate
        cost_plain = plain[0][i] * hour.price - plain[1][i] * hour.rate
        cost = (with_grid[0][i] + with_grid[2][i]) * hour.price - with_grid[1][
            i
        ] * hour.rate
        discharged = alone_purchase - with_grid[0][i]
        for t in targets:
            t.pv_only += pv_only
            t.simulated_pv_only += cost_plain
            t.simulated += cost
            t.discharged += discharged
            t.grid_charged += with_grid[2][i]
            t.hours += 1


def per_year(value: float, days: int) -> float | None:
    """`value` over `days` scaled to a year (None below MIN_DAYS)."""
    if days < MIN_DAYS:
        return None
    return value * YEAR_DAYS / days
