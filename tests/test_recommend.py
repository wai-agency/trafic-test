from datetime import datetime
from zoneinfo import ZoneInfo

from traffic_monitor.recommend import _score, recommend_departure


def test_early_tuesday_scores_high():
    dt = datetime(2026, 8, 4, 4, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert _score(dt) >= 90


def test_friday_afternoon_scores_low():
    dt = datetime(2026, 8, 7, 16, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert _score(dt) <= 40


def test_recommend_contains_slot():
    now = datetime(2026, 8, 3, 8, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    text = recommend_departure(now)
    assert "Empfehlung" in text
    assert "2026" in text
