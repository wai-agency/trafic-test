from traffic_monitor.dashboard import _best_slot, render_html
from traffic_monitor.recommend import TZ
from datetime import datetime


def test_render_html_contains_brand_and_status():
    payload = {
        "generated_at": "2026-08-03T08:00:00+02:00",
        "generated_label": "03.08.2026 08:00",
        "tz": "Europe/Berlin",
        "brand": "BuzimLine",
        "from": "Stuttgart",
        "to": "Bužim",
        "via": "Karawanken",
        "approx_km": 930,
        "status": "warning",
        "status_label": "Erhöhtes Risiko",
        "status_hint": "Test",
        "counts": {"critical": 0, "warning": 1, "total": 1},
        "best_departure": {"label": "Früh", "when": "Di 04.08. 03:00", "iso": "", "score": 100},
        "stops": [("Stuttgart", "Start"), ("Bužim", "Ziel")],
        "alerts": [
            {
                "severity": "warning",
                "source": "ASFINAG",
                "title": "Karawanken Hinweis",
                "detail": "Test detail",
                "location": "Karawanken",
                "url": "https://example.com",
                "delay_min": 20,
            }
        ],
        "sources_down": [],
        "checklist": ["AT-Vignette"],
        "links": [{"label": "HAK", "url": "https://www.hak.hr/"}],
        "recommend_md": "x",
    }
    html = render_html(payload)
    assert "BuzimLine" in html
    assert "Karawanken Hinweis" in html
    assert 'name="viewport"' in html
    assert "viewport-fit=cover" in html


def test_best_slot_future():
    now = datetime(2026, 8, 3, 8, 0, tzinfo=TZ)
    slot = _best_slot(now)
    assert "03:00" in slot["when"] or "04:30" in slot["when"] or "21:00" in slot["when"]
