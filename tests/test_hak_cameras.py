from __future__ import annotations

import traffic_monitor.sources.hak_cameras as hc
from traffic_monitor.dashboard import _payload_from_alerts, render_html
from traffic_monitor.models import Alert

CONFIG = {
    "hak_cameras": {
        "page": "https://m.hak.hr/kamera.asp?g=2&k=177",
        "model": "gemini-2.0-flash",
        "analyze_min_severity": "warning",
        "cams": [
            {"id": 430, "name": "Maljevac — Ausreise HR → BiH", "direction": "HR->BiH", "relevant": True},
            {"id": 429, "name": "Maljevac — Einreise BiH → HR", "direction": "BiH->HR"},
        ],
    }
}


def _gemini_payload(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_parse_gemini_plain_json():
    v = hc.parse_gemini_response(
        _gemini_payload('{"vehicles": 12, "wait_min": 40, "severity": "warning", "summary": "Mittlere Kolonne"}')
    )
    assert v == {"severity": "warning", "summary": "Mittlere Kolonne", "vehicles": 12, "wait_min": 40}


def test_parse_gemini_fenced_json():
    text = "```json\n{\"vehicles\": 25, \"wait_min\": 90, \"severity\": \"critical\", \"summary\": \"Lange Schlange\"}\n```"
    v = hc.parse_gemini_response(_gemini_payload(text))
    assert v["severity"] == "critical"
    assert v["vehicles"] == 25
    assert v["wait_min"] == 90


def test_parse_gemini_bad_severity_defaults_warning():
    v = hc.parse_gemini_response(_gemini_payload('{"vehicles": 3, "severity": "banana"}'))
    assert v["severity"] == "warning"
    assert v["wait_min"] is None


def test_parse_gemini_garbage_returns_none():
    assert hc.parse_gemini_response(_gemini_payload("kein json hier")) is None
    assert hc.parse_gemini_response({"nope": 1}) is None


def test_no_api_key_no_alerts(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert hc.fetch_hak_cameras(CONFIG) == []


def test_fetch_builds_alerts_and_filters_by_severity(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(hc, "_download_image", lambda client, cam_id: b"\xff\xd8\xff")

    verdicts = {
        430: {"severity": "critical", "summary": "Lange Kolonne", "vehicles": 22, "wait_min": 80},
        429: {"severity": "clear", "summary": "Frei", "vehicles": 1, "wait_min": 0},
    }

    def fake_analyze(client, image, api_key, model, name, direction):
        cam_id = 430 if "Ausreise" in name else 429
        return verdicts[cam_id]

    monkeypatch.setattr(hc, "_analyze_with_gemini", fake_analyze)

    alerts = hc.fetch_hak_cameras(CONFIG)
    # 'clear' is below analyze_min_severity=warning → filtered out
    assert len(alerts) == 1
    a = alerts[0]
    assert a.source == "HAK-Cam"
    assert a.severity == "critical"
    assert a.delay_min == 80
    assert a.extras["image_url"] == "https://m.hak.hr/cam.asp?id=430"
    assert "gemini-2.0-flash" in a.detail


def test_cameras_from_config():
    cams = hc.cameras_from_config(CONFIG)
    assert [c["id"] for c in cams] == [430, 429]
    assert cams[0]["relevant"] is True
    assert cams[0]["image_url"].endswith("id=430")


def test_dashboard_embeds_cameras():
    cam_alert = Alert(
        source="HAK-Cam",
        severity="critical",
        title="Kamera: Maljevac — Ausreise HR → BiH",
        detail="Lange Kolonne | KI: gemini-2.0-flash",
        location="Maljevac — Ausreise HR → BiH",
        delay_min=80,
    )
    payload = _payload_from_alerts(CONFIG, [cam_alert])
    assert [c["id"] for c in payload["cameras"]] == [430, 429]
    # Verdict is matched onto the relevant camera by name
    relevant = next(c for c in payload["cameras"] if c["id"] == 430)
    assert relevant["severity"] == "critical"
    assert relevant["wait_min"] == 80

    html_out = render_html(payload)
    assert "Grenz-Kameras" in html_out
    assert "https://m.hak.hr/cam.asp?id=430" in html_out
