"""Time-of-use tariff: windows, weekdays, quarters, price changes, Modul 3,
the form's checks and texts (no Home Assistant).

Windows of the templates from the suppliers' pages (docs/ENTWICKLUNG.md);
the prices are made up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from custom_components.tarifwacht.config_flow import (
    W_DAYS,
    W_PRICE,
    W_QUARTERS,
    W_TIMES,
    open_window_count,
    windows_from_input,
    windows_suggested,
)
from custom_components.tarifwacht.const import (
    BERLIN,
    CONF_BASE_PRICE,
    CONF_CHANGES,
    CONF_FROM,
    CONF_KIND,
    CONF_MODUL3,
    CONF_PRICE,
    CONF_TIME_WINDOWS,
    KIND_TIME_OF_USE,
)
from custom_components.tarifwacht.tariff_config import (
    change_fields,
    price_entity,
    tariff_from_data,
)
from custom_components.tarifwacht.tariff_summary import (
    price_now_attributes,
    summary,
    template_hint,
)
from custom_components.tarifwacht.time_of_use import (
    TEMPLATES,
    day_spans,
    format_times,
    price_from_data,
    windows_error,
)

# Octopus Heat's windows with made-up prices: low, high, standard 30 ct.
HEAT = [
    {
        "price": 20.0,
        "times": "02:00-06:00, 12:00-16:00",
        "days": list(range(7)),
        "quarters": [1, 2, 3, 4],
    },
    {
        "price": 40.0,
        "times": "18:00-21:00",
        "days": list(range(7)),
        "quarters": [1, 2, 3, 4],
    },
]
TARIFF = {
    "key": "heat",
    CONF_KIND: KIND_TIME_OF_USE,
    CONF_PRICE: 30.0,
    CONF_TIME_WINDOWS: HEAT,
    CONF_BASE_PRICE: 12.0,
    "base_period": "month",
}


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=BERLIN).astimezone(UTC)


def window(times: str, price: float = 10.0, days=None, quarters=None) -> dict:
    return {
        "price": price,
        "times": times,
        "days": list(range(7)) if days is None else days,
        "quarters": [1, 2, 3, 4] if quarters is None else quarters,
    }


def price(items: list[dict[str, Any]], moment: datetime, standard: float = 30.0):
    value = price_from_data(standard, items, BERLIN).get(moment)
    return None if value is None else round(value * 100, 6)


def test_day_spans() -> None:
    assert day_spans("02:00-06:00, 12:00-16:00") == [(0, 120, 360), (0, 720, 960)]
    # Across midnight: the part after midnight belongs to the day before.
    assert day_spans("22:00-06:00") == [(0, 1320, 1440), (1, 0, 360)]
    assert day_spans("00:00-24:00") == [(0, 0, 1440)]
    assert day_spans("") == []


def test_octopus_heat_every_day() -> None:
    # Monday 16 November 2026 and Sunday 22 November 2026.
    for day in (16, 22):
        assert price(HEAT, local(2026, 11, day, 1, 45)) == 30.0
        assert price(HEAT, local(2026, 11, day, 2, 0)) == 20.0
        assert price(HEAT, local(2026, 11, day, 5, 45)) == 20.0
        assert price(HEAT, local(2026, 11, day, 6, 0)) == 30.0
        assert price(HEAT, local(2026, 11, day, 12, 0)) == 20.0
        assert price(HEAT, local(2026, 11, day, 17, 45)) == 30.0
        assert price(HEAT, local(2026, 11, day, 18, 0)) == 40.0
        assert price(HEAT, local(2026, 11, day, 20, 45)) == 40.0
        assert price(HEAT, local(2026, 11, day, 21, 0)) == 30.0


def test_ht_nt_weekdays_only() -> None:
    # HT Monday to Friday 06:00-21:00, NT (the standard price) otherwise.
    ht = [window("06:00-21:00", 35.0, days=[0, 1, 2, 3, 4])]
    assert price(ht, local(2026, 11, 20, 10, 0), 25.0) == 35.0  # Friday
    assert price(ht, local(2026, 11, 20, 21, 0), 25.0) == 25.0
    assert price(ht, local(2026, 11, 21, 10, 0), 25.0) == 25.0  # Saturday
    assert price(ht, local(2026, 11, 22, 10, 0), 25.0) == 25.0  # Sunday


def test_window_across_midnight_belongs_to_its_start_day() -> None:
    # Friday nights only: Friday 22:00 to Saturday 06:00.
    friday = [window("22:00-06:00", days=[4])]
    assert price(friday, local(2026, 11, 20, 22, 0)) == 10.0  # Friday
    assert price(friday, local(2026, 11, 21, 3, 0)) == 10.0  # Saturday morning
    assert price(friday, local(2026, 11, 20, 3, 0)) == 30.0  # Friday morning
    assert price(friday, local(2026, 11, 21, 22, 0)) == 30.0  # Saturday night


def test_quarters() -> None:
    winter = [window("00:00-06:00", quarters=[1, 4])]
    assert price(winter, local(2027, 1, 10, 3, 0)) == 10.0
    assert price(winter, local(2026, 7, 10, 3, 0)) == 30.0
    assert price(winter, local(2026, 12, 10, 3, 0)) == 10.0


def test_daylight_saving_follows_the_wall_clock() -> None:
    low = [window("02:00-03:00")]
    # 25 October 2026: 02:00-03:00 twice (00:00 and 01:00 UTC), both low.
    fall = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    assert [price(low, fall + timedelta(minutes=15 * n)) for n in range(8)] == [
        10.0
    ] * 8
    assert price(low, fall + timedelta(hours=2)) == 30.0
    # 28 March 2027: no 02:00-03:00 at all; 01:00 UTC is 03:00 summer time.
    assert price(low, datetime(2027, 3, 28, 0, 45, tzinfo=UTC)) == 30.0
    assert price(low, datetime(2027, 3, 28, 1, 0, tzinfo=UTC)) == 30.0


def test_first_window_wins_and_zero_is_a_price() -> None:
    free = [window("10:00-12:00", 0.0)]
    assert price(free, local(2026, 11, 22, 11, 0)) == 0.0


def test_windows_error() -> None:
    assert windows_error(HEAT) is None
    assert windows_error([window("02:00-06:00"), {**window("x"), "price": None}]) == (
        2,
        "tou_price_required",
    )
    assert windows_error([window("2-6")]) == (1, "tou_window_format")
    assert windows_error([window("02:10-06:00")]) == (1, "tou_window_grid")
    assert windows_error([window("")]) == (1, "tou_times_required")
    assert windows_error([window("02:00-06:00", days=[])]) == (1, "tou_days_required")
    assert windows_error([window("02:00-06:00", quarters=[])]) == (
        1,
        "tou_quarters_required",
    )
    # Overlap on the same day, also within one window.
    assert windows_error([window("02:00-06:00"), window("05:00-07:00")]) == (
        2,
        "tou_overlap",
    )
    assert windows_error([window("02:00-06:00, 05:00-07:00")]) == (1, "tou_overlap")
    # The same times on other weekdays or in other quarters: fine.
    assert (
        windows_error(
            [
                window("21:00-06:00", 20.0, days=[0, 1, 2, 3, 4]),
                window("00:00-24:00", 20.0, days=[5, 6]),
            ]
        )
        == (2, "tou_overlap")  # Friday night runs into Saturday
    )
    assert (
        windows_error(
            [
                window("06:00-21:00", 35.0, days=[0, 1, 2, 3, 4]),
                window("06:00-21:00", 30.0, days=[5, 6]),
            ]
        )
        is None
    )
    assert (
        windows_error(
            [window("02:00-06:00", quarters=[1]), window("02:00-06:00", quarters=[2])]
        )
        is None
    )


def test_tariff_with_price_change_and_base_price() -> None:
    data = {
        **TARIFF,
        CONF_CHANGES: [
            {
                CONF_FROM: "2027-01-01",
                CONF_PRICE: 32.0,
                CONF_TIME_WINDOWS: [window("00:00-05:00", 18.0)],
                CONF_BASE_PRICE: 14.0,
                "base_period": "month",
            }
        ],
    }
    tariff = tariff_from_data(data, lambda _entity: None)
    assert tariff.price(local(2026, 12, 31, 3, 0)) == pytest.approx(0.20)
    assert tariff.price(local(2027, 1, 1, 3, 0)) == pytest.approx(0.18)
    assert tariff.price(local(2027, 1, 1, 13, 0)) == pytest.approx(0.32)
    assert tariff.at(local(2027, 1, 2)).base_price == 14.0
    assert price_entity(data) is None
    assert "time_windows" in change_fields(KIND_TIME_OF_USE)


def test_modul3_on_top() -> None:
    data = {
        **TARIFF,
        CONF_MODUL3: {
            "active": True,
            "nt": 3.0,
            "ht": 13.0,
            "quarters": [1, 2, 3, 4],
            "nt_windows": "00:00-06:00",
            "ht_windows": "17:00-20:00",
            "standard": 8.0,
            "vat": 19.0,
        },
    }
    tariff = tariff_from_data(data, lambda _entity: None)
    # Low window and Modul 3 NT: 20 + (3 - 8) * 1.19.
    assert tariff.price(local(2026, 11, 22, 3, 0)) * 100 == pytest.approx(14.05)
    # High window and Modul 3 HT: 40 + (13 - 8) * 1.19.
    assert tariff.price(local(2026, 11, 22, 19, 0)) * 100 == pytest.approx(45.95)
    # Standard price, standard grid fee.
    assert tariff.price(local(2026, 11, 22, 10, 0)) * 100 == pytest.approx(30.0)


def test_summary_and_attributes() -> None:
    text = summary(TARIFF, date(2026, 11, 22), german=True)
    assert text.startswith(
        "**Zeitfenster-Tarif** · heute: 20 ct 02:00–06:00, 12:00–16:00; "
        "40 ct 18:00–21:00; sonst 30 ct/kWh + 12,00 €/Monat"
    )
    weekdays = {
        **TARIFF,
        CONF_TIME_WINDOWS: [window("06:00-21:00", 35, [0, 1, 2, 3, 4], [1, 4])],
    }
    english = summary(weekdays, date(2026, 11, 22), german=False)
    assert "35 ct 06:00–21:00 (Mon–Fri, Q1, Q4); otherwise 30 ct/kWh" in english
    attributes = price_now_attributes(TARIFF, local(2026, 11, 22, 19, 0))
    assert attributes["time_window"] == 2
    assert (
        price_now_attributes(TARIFF, local(2026, 11, 22, 10, 0))["time_window"] is None
    )


def test_format_times() -> None:
    assert format_times("22:00-06:00,12:00 - 16:00") == "22:00–06:00, 12:00–16:00"
    assert format_times("nonsense") == "nonsense"


def test_templates_are_valid() -> None:
    for key, (name, items) in TEMPLATES.items():
        filled = [{**window(""), **item} for item in items]
        assert windows_error(filled) is None, key
        assert name
        assert template_hint(key, german=True).startswith("\n\nVorlage")
        assert template_hint(key, german=False).startswith("\n\nTemplate")
    assert template_hint("", german=True) == ""


def test_form_windows() -> None:
    form = {
        "window_1": {
            W_PRICE: 20,
            W_TIMES: " 02:00-06:00 ",
            W_DAYS: ["0", "1"],
            W_QUARTERS: ["1", "2", "3", "4"],
        },
        # Neither price nor times: left out.
        "window_2": {W_DAYS: ["0"], W_QUARTERS: ["1"]},
        "window_3": {W_PRICE: 40, W_TIMES: "18:00-21:00"},
    }
    items, errors = windows_from_input(form, [])
    assert errors == {}
    assert items == [
        {
            "price": 20.0,
            "times": "02:00-06:00",
            "days": [0, 1],
            "quarters": [1, 2, 3, 4],
        },
        {
            "price": 40.0,
            "times": "18:00-21:00",
            "days": list(range(7)),
            "quarters": [1, 2, 3, 4],
        },
    ]
    # The error names the section of the window.
    form["window_3"][W_TIMES] = "01:00-03:00"
    form["window_1"][W_DAYS] = [str(d) for d in range(7)]
    assert windows_from_input(form, [])[1] == {"window_3": "tou_overlap"}
    assert windows_from_input({"window_1": {}}, [])[1] == {
        "window_1": "tou_window_required"
    }
    # A section that did not come back keeps the stored window.
    assert windows_from_input({}, HEAT) == (HEAT, {})
    suggested = windows_suggested(HEAT)
    assert suggested["window_2"][W_TIMES] == "18:00-21:00"
    assert suggested["window_3"] == {
        W_DAYS: [str(d) for d in range(7)],
        W_QUARTERS: ["1", "2", "3", "4"],
    }
    assert open_window_count(HEAT) == 2
    assert open_window_count([]) == 1
