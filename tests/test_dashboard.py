from traffic_monitor.dashboard import _best_slot, render_html
from traffic_monitor.recommend import TZ
from datetime import datetime


def _base_payload(**overrides):
    payload = {
        "generated_at": "2026-08-03T08:00:00+02:00",
        "generated_label": "03.08.2026 08:00",
        "tz": "Europe/Berlin",
        "brand": "BuzimLine",
        "from": "Waiblingen",
        "to": "Bužim",
        "via": "Karawanken",
        "approx_km": 960,
        "status": "warning",
        "status_label": "Erhöhtes Risiko",
        "status_hint": "Test",
        "counts": {"critical": 0, "warning": 1, "total": 1},
        "best_departure": {"label": "Früh", "when": "Di 04.08. 03:00", "iso": "", "score": 100},
        "perfect": None,
        "stops": [("Waiblingen", "Start"), ("Bužim", "Ziel")],
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
    payload.update(overrides)
    return payload


def test_render_html_contains_brand_and_status():
    html = render_html(_base_payload())
    assert "BuzimLine" in html
    assert "Karawanken Hinweis" in html
    assert 'name="viewport"' in html
    assert "viewport-fit=cover" in html
    assert "Perfekte Abfahrt" in html


def test_render_html_perfect_block():
    perfect = {
        "generated_label": "03.08.2026 10:12",
        "provider": "Google Live-Verkehr",
        "hint": "Test hinweis",
        "best": {
            "badge": "früheste Ankunft",
            "depart_label": "Mon 03.08. 15:00",
            "depart_short": "15:00",
            "depart_day": "Mon 03.08.",
            "arrive_border_short": "01:40",
            "arrive_buzim_short": "02:51",
            "border_wait_min": 27,
            "border_cars": 3.4,
            "total_label": "11h 51min",
            "distance_km": 963,
            "route_id": "primary",
            "route_title": "Hauptroute",
            "route_summary": "Waiblingen → Maljevac → Bužim",
            "maps_url": "https://www.google.com/maps/dir/?api=1&origin=Waiblingen",
            "stops": [
                "Waiblingen, Germany",
                "Salzburg, Austria",
                "Villach, Austria",
                "Zagreb, Croatia",
                "Maljevac, Croatia",
                "Bužim, Bosnia and Herzegovina",
            ],
            "notes": "Forecast",
        },
        "best_low_border": {
            "depart_label": "Mon 03.08. 16:30",
            "depart_short": "16:30",
            "arrive_border_short": "03:10",
            "arrive_buzim_short": "04:12",
            "border_wait_min": 18,
            "border_cars": 2.2,
            "maps_url": "https://www.google.com/maps",
            "route_title": "Hauptroute",
        },
        "timeline": [
            {
                "depart_day": "Mon",
                "depart_short": "15:00",
                "arrive_buzim_short": "02:51",
                "border_wait_min": 27,
                "border_cars": 3.4,
                "total_label": "11h 51min",
                "load": "ok",
                "is_best": True,
            }
        ],
        "top": [
            {
                "depart_short": "15:00",
                "depart_day": "Mon 03.08.",
                "arrive_border_short": "01:40",
                "arrive_buzim_short": "02:51",
                "border_wait_min": 27,
                "route_id": "primary",
                "total_label": "11h 51min",
            }
        ],
    }
    html = render_html(_base_payload(perfect=perfect))
    assert "15:00" in html
    assert "In Google Maps öffnen" in html
    assert "Freiere Grenze" in html
    assert "Abfahrtsfenster" in html
    assert "perfect-time" in html


def test_best_slot_future():
    now = datetime(2026, 8, 3, 8, 0, tzinfo=TZ)
    slot = _best_slot(now)
    assert "03:00" in slot["when"] or "04:30" in slot["when"] or "21:00" in slot["when"]
