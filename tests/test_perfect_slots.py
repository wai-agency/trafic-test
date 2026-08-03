from datetime import datetime
from zoneinfo import ZoneInfo

from traffic_monitor.perfect_depart import departure_slots

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
