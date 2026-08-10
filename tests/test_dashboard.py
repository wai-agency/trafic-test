from traffic_monitor.dashboard import _best_slot, render_html
from traffic_monitor.recommend import TZ
from datetime import datetime


def _base_payload(**overrides):
    payload = {
        "generated_at": "2026-08-03T08:00:00+02:00",
        "generated_label": "03.08.2026 08:00",
        "tz": "Europe/Berlin",
        "brand": "BuzimLine",
        "from": "Bužim",
        "to": "Waiblingen",
        "via": "Karawanken",
        "approx_km": 960,
        "status": "warning",
        "status_label": "Erhöhtes Risiko",
        "status_hint": "Test",
        "counts": {"critical": 0, "warning": 1, "total": 1},
        "best_departure": {"label": "Früh", "when": "Di 04.08. 03:00", "iso": "", "score": 100},
        "perfect": None,
        "maljevac_now": {
            "name": "Maljevac",
            "cars": 10,
            "wait_min": 15,
            "source": "HAK-Cam · gpt-5.6-terra",
            "note": "Live gezählt von HAK-Kameras (OpenAI Vision)",
            "stale": False,
            "trucks": 1,
            "to_bih": {
                "name": "Maljevac — Ausreise HR → BiH",
                "direction": "HR->BiH",
                "cars": 10,
                "wait_min": 15,
                "trucks": 1,
                "severity": "warning",
                "cam_id": 430,
                "note": "Kurze Schlange Richtung BiH",
            },
            "to_hr": {
                "name": "Maljevac — Einreise BiH → HR",
                "direction": "BiH->HR",
                "cars": 4,
                "wait_min": 8,
                "trucks": 0,
                "severity": "info",
                "cam_id": 429,
                "note": "Wenig Verkehr Richtung HR",
            },
        },
        "borders": [{"name": "Maljevac", "cars": 10, "wait_min": 15, "stale": False}],
        "stops": [("Bužim", "Start"), ("Waiblingen", "Ziel")],
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
    assert 'id="stale-banner"' in html
    assert 'data-generated-at="2026-08-03T08:00:00+02:00"' in html
    assert "Daten veraltet" in html
    assert "Maljevac jetzt" in html
    assert "Eure Richtung (BiH → HR)" in html
    assert "Gegenrichtung (HR → BiH)" in html
    assert "Ausreise HR (eure Richtung)" in html
    assert "Gegenrichtung BiH" in html
    assert ">10<" in html or "10 Autos" in html
    assert ">4<" in html or "4 Autos" in html
    assert "HAK-Cam" in html
    assert 'border-side sev-warning' in html
    assert 'border-side sev-clear' in html
    assert 'metric sev-warning' in html
    assert 'metric sev-clear' in html
    assert "side-sev sev-warning" in html
    assert ">Erhöht<" in html
    assert ">Frei<" in html
    assert "border-now is-warning" in html


def test_render_html_critical_severity_styling():
    html = render_html(
        _base_payload(
            status="critical",
            status_label="Stau / Störung",
            maljevac_now={
                "name": "Maljevac",
                "cars": 23,
                "wait_min": 69,
                "source": "HAK-Cam · gpt-5.6-terra",
                "note": "Live",
                "stale": False,
                "to_bih": {
                    "name": "Ausreise",
                    "cars": 23,
                    "wait_min": 69,
                    "trucks": 0,
                    "severity": "critical",
                    "queue_end_visible": False,
                    "note": "Worst Case",
                },
                "to_hr": {
                    "name": "Einreise",
                    "cars": 2,
                    "wait_min": 5,
                    "trucks": 0,
                    "severity": "info",
                },
            },
        )
    )
    assert "hero status-critical" in html
    assert "border-now is-critical" in html
    assert "border-side sev-critical" in html
    assert "metric sev-critical" in html
    assert ">Stau<" in html
    assert "border-side sev-clear" in html
    assert ">Frei<" in html


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
            "route_summary": "Bužim → Maljevac → Waiblingen",
            "maps_url": "https://www.google.com/maps/dir/?api=1&origin=Bu%C5%BEim",
            "stops": [
                "Bužim, Bosnia and Herzegovina",
                "Maljevac, Croatia",
                "Zagreb, Croatia",
                "Villach, Austria",
                "Salzburg, Austria",
                "Waiblingen, Germany",
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
    assert "Freiere Grenze" in html or "freiere Grenze" in html
    assert "früheste Ankunft" in html
    assert "nicht die leerste Grenze" in html
    assert "Abfahrtsfenster" in html
    assert "perfect-time" in html
    assert "perfect-goal" in html


def test_perfect_explains_high_border_wait():
    perfect = {
        "generated_label": "10.08.2026 13:13",
        "hint": "alt",
        "best": {
            "badge": "früheste Ankunft",
            "depart_label": "Mon 10.08. 14:00",
            "depart_short": "14:00",
            "depart_day": "Mon 10.08.",
            "arrive_border_short": "14:43",
            "arrive_buzim_short": "02:26",
            "border_wait_min": 53,
            "border_cars": 6.6,
            "total_label": "12h 26min",
            "distance_km": 967,
            "route_id": "primary",
            "maps_url": "https://www.google.com/maps",
            "stops": ["Bužim", "Maljevac", "Zagreb", "Waiblingen"],
        },
        "best_low_border": None,
        "timeline": [
            {
                "depart_day": "Mon",
                "depart_short": "14:00",
                "arrive_buzim_short": "02:26",
                "border_wait_min": 53,
                "border_cars": 6.6,
                "load": "voll",
                "is_best": True,
            },
            {
                "depart_day": "Tue",
                "depart_short": "03:00",
                "arrive_buzim_short": "14:49",
                "border_wait_min": 18,
                "border_cars": 2.2,
                "load": "frei",
                "is_best": False,
            },
        ],
        "top": [],
    }
    html = render_html(_base_payload(perfect=perfect))
    assert "53 min an der Grenze können trotzdem" in html
    assert "03:00" in html
    assert "18 min" in html or "~18 min" in html
    assert "Alternative · freiere Grenze" in html
    assert "Spart ~35 min an der Grenze" in html


def test_best_slot_future():
    now = datetime(2026, 8, 3, 8, 0, tzinfo=TZ)
    slot = _best_slot(now)
    assert "03:00" in slot["when"] or "04:30" in slot["when"] or "21:00" in slot["when"]


def test_stau_zeitachse_maps_alerts_and_renders():
    from traffic_monitor.dashboard import build_stau_zeitachse
    from traffic_monitor.models import Alert

    alerts = [
        Alert(
            source="Nakordoni",
            severity="critical",
            title="Grenze: Maljevac",
            detail="lang",
            location="Maljevac",
            delay_min=80,
        ),
        Alert(
            source="GPMaljevac",
            severity="critical",
            title="Kolaps u Sloveniji",
            detail="kolone",
            location="Slowenien",
        ),
        Alert(
            source="autobahn.de",
            severity="critical",
            title="A8 | Aichelberg",
            detail="Kontrolle",
            location="Stuttgart -> München",
        ),
    ]
    mj = {
        "to_hr": {
            "cars": 20,
            "wait_min": 50,
            "severity": "critical",
            "queue_end_visible": False,
        }
    }
    perfect = {
        "timeline": [
            {
                "depart_short": "12:00",
                "depart_day": "Sun",
                "arrive_border_short": "12:40",
                "border_wait_min": 90,
                "border_cars": 12.0,
                "load": "voll",
                "is_best": True,
                "arrive_buzim_short": "01:00",
            }
        ]
    }
    axis = build_stau_zeitachse(alerts, maljevac_now=mj, perfect=perfect)
    by_id = {s["id"]: s for s in axis["segments"]}
    assert by_id["maljevac"]["severity"] == "critical"
    assert by_id["karawanken"]["severity"] == "critical"
    assert by_id["a8"]["severity"] == "critical"
    assert by_id["buzim"]["severity"] == "clear"
    assert by_id["lucko"]["severity"] == "clear"
    assert axis["border_hours"][0]["load"] == "voll"

    html = render_html(_base_payload(stau_zeitachse=axis, perfect=perfect))
    assert "Stau-Zeitachse" in html
    assert "axis-corridor" in html
    assert "Grenze Maljevac nach Abfahrt" in html
    assert "axis-hour" in html


def test_stau_zeitachse_overlays_lucko():
    from traffic_monitor.dashboard import build_stau_zeitachse

    axis = build_stau_zeitachse(
        [],
        lucko_now={
            "entry": {
                "cars": 18,
                "wait_min": 35,
                "severity": "warning",
                "queue_end_visible": True,
            }
        },
    )
    by_id = {s["id"]: s for s in axis["segments"]}
    assert by_id["lucko"]["severity"] == "warning"
    assert by_id["lucko"]["delay_min"] == 35
    assert "18" in by_id["lucko"]["summary"]


def test_render_html_lucko_section():
    html = render_html(
        _base_payload(
            lucko_now={
                "name": "Lučko",
                "source": "HAK-Cam · gpt-5.6-terra",
                "entry": {
                    "name": "Lučko — Ulaz +1 km",
                    "cars": 12,
                    "wait_min": 28,
                    "severity": "warning",
                    "note": "Kolonne vor Maut",
                },
            }
        )
    )
    assert "Lučko jetzt" in html
    assert "12" in html
    assert "28" in html
    assert "A1" in html
