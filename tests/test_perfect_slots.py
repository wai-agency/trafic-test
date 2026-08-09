from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from traffic_monitor.perfect_depart import advance_perfect_payload, departure_slots

TZ = ZoneInfo("Europe/Berlin")


def test_departure_slots_include_night_hours():
    now = datetime(2026, 8, 3, 11, 20, tzinfo=TZ)
    slots = departure_slots(now)
    hours = {(s.date(), s.hour, s.minute) for s in slots}

    # Tonight / early tomorrow night window
    assert (datetime(2026, 8, 4, 0, 0, tzinfo=TZ).date(), 0, 0) in hours
    assert (datetime(2026, 8, 4, 1, 0, tzinfo=TZ).date(), 1, 0) in hours
    assert (datetime(2026, 8, 4, 2, 0, tzinfo=TZ).date(), 2, 0) in hours
    assert (datetime(2026, 8, 4, 3, 0, tzinfo=TZ).date(), 3, 0) in hours
    assert (datetime(2026, 8, 4, 4, 0, tzinfo=TZ).date(), 4, 0) in hours

    # Afternoon dense sampling
    assert any(s.hour == 15 and s.minute in (0, 30) for s in slots)
    assert len(slots) >= 20


def test_advance_perfect_rolls_past_best():
    payload = {
        "best": {
            "depart": "2026-08-03T13:00:00+02:00",
            "depart_short": "13:00",
            "depart_day": "Mon 03.08.",
            "depart_label": "Mon 03.08. 13:00",
            "arrive_buzim": "2026-08-04T00:51:00+02:00",
            "arrive_buzim_short": "00:51",
            "arrive_border_short": "23:40",
            "border_wait_min": 27,
            "border_cars": 3.4,
            "total_label": "11h 51min",
            "distance_km": 963,
            "route_id": "primary",
            "maps_url": "https://maps.example/a",
            "badge": "früheste Ankunft",
        },
        "top": [
            {
                "depart": "2026-08-03T13:00:00+02:00",
                "depart_short": "13:00",
                "depart_day": "Mon 03.08.",
                "depart_label": "Mon 03.08. 13:00",
                "arrive_buzim": "2026-08-04T00:51:00+02:00",
                "arrive_buzim_short": "00:51",
                "arrive_border_short": "23:40",
                "border_wait_min": 27,
                "border_cars": 3.4,
                "total_label": "11h 51min",
                "distance_km": 963,
                "route_id": "primary",
                "maps_url": "https://maps.example/a",
            },
            {
                "depart": "2026-08-03T14:00:00+02:00",
                "depart_short": "14:00",
                "depart_day": "Mon 03.08.",
                "depart_label": "Mon 03.08. 14:00",
                "arrive_buzim": "2026-08-04T01:40:00+02:00",
                "arrive_buzim_short": "01:40",
                "arrive_border_short": "00:30",
                "border_wait_min": 20,
                "border_cars": 2.8,
                "total_label": "11h 40min",
                "distance_km": 963,
                "route_id": "primary",
                "maps_url": "https://maps.example/b",
            },
        ],
        "timeline": [
            {
                "depart": "2026-08-03T13:00:00+02:00",
                "depart_short": "13:00",
                "depart_day": "Mon",
                "is_best": True,
                "border_wait_min": 27,
                "arrive_buzim_short": "00:51",
                "load": "ok",
            },
            {
                "depart": "2026-08-03T14:00:00+02:00",
                "depart_short": "14:00",
                "depart_day": "Mon",
                "is_best": False,
                "border_wait_min": 20,
                "arrive_buzim_short": "01:40",
                "load": "frei",
            },
        ],
        "hint": "Test",
    }
    now = datetime(2026, 8, 3, 13, 10, tzinfo=TZ)
    out = advance_perfect_payload(payload, now=now)
    assert out["best"]["depart_short"] == "14:00"
    assert out["best"]["badge"] == "nächste beste"
    assert out["advanced"] is True
    assert all(t["depart_short"] != "13:00" for t in out["timeline"])
    assert any(t.get("is_best") for t in out["timeline"])


def test_advance_perfect_keeps_future_best():
    payload = {
        "best": {
            "depart": "2026-08-03T15:00:00+02:00",
            "depart_short": "15:00",
            "arrive_buzim": "2026-08-04T02:00:00+02:00",
            "badge": "früheste Ankunft",
            "border_cars": 6.0,
        },
        "top": [
            {
                "depart": "2026-08-03T15:00:00+02:00",
                "depart_short": "15:00",
                "arrive_buzim": "2026-08-04T02:00:00+02:00",
                "border_cars": 6.0,
                "border_wait_min": 40,
            },
            {
                "depart": "2026-08-03T16:00:00+02:00",
                "depart_short": "16:00",
                "arrive_buzim": "2026-08-04T03:10:00+02:00",
                "border_cars": 2.0,
                "border_wait_min": 15,
            },
        ],
        "timeline": [],
    }
    now = datetime(2026, 8, 3, 11, 0, tzinfo=TZ)
    out = advance_perfect_payload(payload, now=now)
    assert out["best"]["depart_short"] == "15:00"
    assert out["advanced"] is False
    assert out["best_low_border"]["depart_short"] == "16:00"


def test_score_slot_adds_near_term_lucko_wait(monkeypatch):
    from traffic_monitor.perfect_depart import score_slot
    from traffic_monitor.reroute import ROUTES

    monkeypatch.setattr(
        "traffic_monitor.perfect_depart.osrm_leg_times",
        lambda _route: (45 * 60, 11 * 3600, 960_000),
    )
    now = datetime(2026, 8, 3, 12, 0, tzinfo=TZ)
    depart = now + timedelta(minutes=30)
    scored = score_slot(
        depart,
        ROUTES["primary"],
        forecast=[],
        google_key=None,
        live_lucko_wait_min=40,
        now=now,
    )
    assert scored is not None
    assert scored.lucko_wait_min == 40
    assert "Lučko" in scored.notes
    # Far departure: live Lučko should not apply (>5h to Lučko)
    far = score_slot(
        now + timedelta(hours=12),
        ROUTES["primary"],
        forecast=[],
        google_key=None,
        live_lucko_wait_min=40,
        now=now,
    )
    assert far is not None
    assert far.lucko_wait_min == 0
