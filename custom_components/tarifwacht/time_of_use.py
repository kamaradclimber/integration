"""Time-of-use tariff: fixed energy prices per time of day. Pure Python.

A standard energy price plus up to four time windows, each with its own
price, clock times, weekdays and calendar quarters. Examples (sources in
docs/ENTWICKLUNG.md): Octopus Heat - low 02:00-06:00 and 12:00-16:00, high
18:00-21:00, standard otherwise; a two-rate tariff (HT/NT) - HT Monday to
Friday 06:00-21:00, NT otherwise.

Times are wall-clock times in German local time on the quarter-hour grid,
written like the Modul 3 windows (modul3.parse_windows). A window across
midnight belongs to the day it starts on: "22:00-06:00" on Fridays includes
Saturday 00:00-06:00, not Monday 00:00-06:00. Public holidays are not
modelled. Prices include VAT (ct/kWh as on the contract, EUR/kWh here).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from itertools import pairwise
from typing import Any

from .modul3 import WindowError, parse_windows

MAX_WINDOWS = 4
# Monday = 0, as date.weekday().
WEEKDAYS = tuple(range(7))
ALL_QUARTERS = (1, 2, 3, 4)

# Keys of a stored time window.
KEY_PRICE = "price"
KEY_TIMES = "times"
KEY_DAYS = "days"
KEY_QUARTERS = "quarters"

# Time windows of known tariffs, without prices (they depend on the postcode
# or the grid area): key -> (suggested name, windows). Checked 16.11.2026,
# sources in docs/ENTWICKLUNG.md. tariff_summary.template_hint says which
# window is which price level.
TEMPLATES: dict[str, tuple[str, list[dict[str, Any]]]] = {
    "octopus_heat": (
        "Octopus Heat",
        [{KEY_TIMES: "02:00-06:00, 12:00-16:00"}, {KEY_TIMES: "18:00-21:00"}],
    ),
    "octopus_go": ("Octopus Go", [{KEY_TIMES: "00:00-05:00"}]),
    "enbw_sparzeit": ("EnBW SparZeit", [{KEY_TIMES: "00:00-06:00, 10:00-16:00"}]),
    # HT on weekdays, NT (the standard price) at night and at the weekend;
    # the times of the Stadtwerke München grid, others differ.
    "ht_nt": ("HT/NT", [{KEY_TIMES: "06:00-21:00", KEY_DAYS: [0, 1, 2, 3, 4]}]),
}


def _quarter(day: date) -> int:
    return (day.month - 1) // 3 + 1


def day_spans(text: str) -> list[tuple[int, int, int]]:
    """ "22:00-06:00, 12:00-16:00" -> [(0, 1320, 1440), (1, 0, 360),
    (0, 720, 960)]: (day offset, start, end) in minutes of the local day.
    Offset 1 is the part of a window after midnight: it belongs to the day
    before. Raises WindowError like modul3.parse_windows."""
    spans: list[tuple[int, int, int]] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        windows = parse_windows(part)
        if len(windows) == 2:  # across midnight
            (a, b), (c, d) = windows
            spans += [(0, a, b), (1, c, d)]
        else:
            spans.append((0, *windows[0]))
    return spans


@dataclass(frozen=True)
class TimeWindow:
    """One price level: its price in EUR/kWh where it applies."""

    price: float
    spans: tuple[tuple[int, int, int], ...]
    days: frozenset[int]
    quarters: frozenset[int]

    def covers(self, local: datetime) -> bool:
        """Whether the window applies at the local time `local`."""
        minute = local.hour * 60 + local.minute
        for offset, start, end in self.spans:
            if start <= minute < end:
                day = local.date() - timedelta(days=offset)
                if day.weekday() in self.days and _quarter(day) in self.quarters:
                    return True
        return False


def windows_from_data(items: Iterable[Mapping[str, Any]]) -> tuple[TimeWindow, ...]:
    """The stored windows (ct/kWh) as TimeWindow (EUR/kWh); a window whose
    times cannot be read is left out (the form never stores one)."""
    windows: list[TimeWindow] = []
    for item in items:
        try:
            spans = day_spans(str(item.get(KEY_TIMES) or ""))
        except WindowError:
            continue
        windows.append(
            TimeWindow(
                price=float(item.get(KEY_PRICE) or 0) / 100,
                spans=tuple(spans),
                days=frozenset(int(d) for d in item.get(KEY_DAYS, WEEKDAYS)),
                quarters=frozenset(
                    int(q) for q in item.get(KEY_QUARTERS, ALL_QUARTERS)
                ),
            )
        )
    return tuple(windows)


@dataclass(frozen=True)
class TimeOfUsePrice:
    """Price per quarter hour: the first window that applies, else the
    standard price. EUR/kWh incl. VAT."""

    standard: float
    windows: tuple[TimeWindow, ...]
    tz: tzinfo

    def window_at(self, slot: datetime) -> int | None:
        """Index of the window applying at `slot`, None: the standard price."""
        local = slot.astimezone(self.tz)
        return next(
            (i for i, window in enumerate(self.windows) if window.covers(local)),
            None,
        )

    def get(self, slot: datetime) -> float | None:
        index = self.window_at(slot)
        return self.standard if index is None else self.windows[index].price


def price_from_data(
    standard_ct: float, items: Iterable[Mapping[str, Any]], tz: tzinfo
) -> TimeOfUsePrice:
    """The price source of a stored time-of-use tariff."""
    return TimeOfUsePrice(standard_ct / 100, windows_from_data(items), tz)


def windows_error(items: Sequence[Mapping[str, Any]]) -> tuple[int, str] | None:
    """(number of the window from 1, error key) of the first window that
    cannot be used, None if all are fine.

    Each window needs a price (0 is a price), readable times on the
    quarter-hour grid, a weekday and a quarter. Two windows (or two parts of
    one) may not apply at the same time on the same weekday in the same
    quarter: which price would count would be a guess.
    """
    spans: list[list[tuple[int, int, int]]] = []
    for number, item in enumerate(items, 1):
        if item.get(KEY_PRICE) is None:
            return number, "tou_price_required"
        try:
            found = day_spans(str(item.get(KEY_TIMES) or ""))
        except WindowError as err:
            return number, f"tou_{err.key}"
        if not found:
            return number, "tou_times_required"
        if not item.get(KEY_DAYS):
            return number, "tou_days_required"
        if not item.get(KEY_QUARTERS):
            return number, "tou_quarters_required"
        spans.append(found)
    for weekday in WEEKDAYS:
        for quarter in ALL_QUARTERS:
            active: list[tuple[int, int, int]] = []
            for number, (item, found) in enumerate(zip(items, spans, strict=True), 1):
                if quarter not in {int(q) for q in item[KEY_QUARTERS]}:
                    continue
                days = {int(d) for d in item[KEY_DAYS]}
                active += [
                    (start, end, number)
                    for offset, start, end in found
                    if (weekday - offset) % 7 in days
                ]
            for a, b in pairwise(sorted(active)):
                if b[0] < a[1]:
                    return max(a[2], b[2]), "tou_overlap"
    return None


def format_times(text: str) -> str:
    """The times as read, "02:00-06:00, 12:00-16:00"; the text itself if
    it cannot be read."""
    try:
        parts = []
        for part in filter(None, (p.strip() for p in text.split(","))):
            windows = parse_windows(part)
            start = windows[0][0]
            end = windows[-1][1]
            parts.append(
                f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}"
            )
    except WindowError:
        return text
    return ", ".join(parts)
