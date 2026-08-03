from __future__ import annotations

import traffic_monitor.sources.hak_cameras as hc
from traffic_monitor.dashboard import _payload_from_alerts, render_html
from traffic_monitor.models import Alert

CONFIG = {
    "hak_cameras": {
        "page": "https://m.hak.hr/kamera.asp?g=2&k=177",
        "model": "gpt-4o-mini",
        "analyze_min_severity": "warning",
        "cams": [
            {
                "id": 430,
                "name": "Maljevac — Ausreise HR → BiH",
                "direction": "HR->BiH",
                "relevant": True,
                "analyze": True,
                "role": "to_bih",
            },
            {
                "id": 429,
                "name": "Maljevac — Einreise BiH → HR",
                "direction": "BiH->HR",
                "relevant": True,
                "analyze": True,
                "role": "to_hr",
            },
        ],
    }
}


def _openai_payload(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


def test_parse_openai_plain_json():
    v = hc.parse_vision_response(
        _openai_payload(
            '{"vehicles": 12, "trucks": 1, "wait_min": 40, "severity": "warning", '
            '"weather": "sunny", "road": "stockend", "summary": "Mittlere Kolonne", '
            '"queue_end_visible": true}'
        )
    )
    assert v["severity"] == "warning"
    assert v["vehicles"] == 12
    assert v["trucks"] == 1
    assert v["wait_min"] == 40
    assert v["weather"] == "sunny"
    assert v["road"] == "stockend"
    assert v["queue_end_visible"] is True


def test_queue_end_not_visible_raises_floor():
    v = hc.parse_vision_response(
        _openai_payload(
            '{"vehicles": 8, "trucks": 1, "wait_min": 10, "severity": "clear", '
            '"weather": "sunny", "road": "flüssig", "summary": "Ein paar Autos", '
            '"queue_end_visible": false}'
        )
    )
    assert v["queue_end_visible"] is False
    assert v["severity"] == "warning"
    assert v["wait_min"] >= 15
    assert v["vehicles"] == 8  # do not invent extra cars in post-process
    assert v["road"] == "stockend"
    assert "ende" in v["summary"].lower()


def test_queue_end_not_visible_many_cars_critical():
    v = hc.normalize_verdict(
        {
            "vehicles": 28,
            "wait_min": 20,
            "severity": "warning",
            "road": "dicht",
            "summary": "Lange Schlange",
            "queue_end_visible": False,
        }
    )
    assert v["severity"] == "critical"
    assert v["wait_min"] >= 40
    assert v["vehicles"] == 28


def test_queue_end_visible_keeps_modest_count():
    v = hc.normalize_verdict(
        {
            "vehicles": 12,
            "wait_min": 20,
            "severity": "warning",
            "road": "stockend",
            "summary": "Mittlere Schlange bis zur Kabine",
            "queue_end_visible": True,
        }
    )
    assert v["queue_end_visible"] is True
    assert v["severity"] == "warning"
    assert v["vehicles"] == 12
    assert v["wait_min"] == 20


def test_parse_openai_fenced_json():
    text = "```json\n{\"vehicles\": 25, \"wait_min\": 90, \"severity\": \"critical\", \"summary\": \"Lange Schlange\"}\n```"
    v = hc.parse_vision_response(_openai_payload(text))
    assert v["severity"] == "critical"
    assert v["vehicles"] == 25
    assert v["wait_min"] == 90


def test_parse_bad_severity_defaults_warning():
    v = hc.parse_vision_response(_openai_payload('{"vehicles": 3, "severity": "banana"}'))
    assert v["severity"] == "warning"
    assert v["wait_min"] is None


def test_parse_garbage_returns_none():
    assert hc.parse_vision_response(_openai_payload("kein json hier")) is None
    assert hc.parse_vision_response({"nope": 1}) is None


def test_no_api_key_no_alerts(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert hc.fetch_hak_cameras(CONFIG) == []


def test_fetch_builds_alerts_including_clear_as_info(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(hc, "_CACHE_PATH", tmp_path / "cam_cache.json")
    monkeypatch.setattr(hc, "_download_image", lambda client, cam_id: b"\xff\xd8\xff")

    verdicts = {
        430: {
            "severity": "critical",
            "summary": "Lange Kolonne",
            "vehicles": 22,
            "trucks": 3,
            "wait_min": 80,
            "weather": "sunny",
            "road": "dicht",
        },
        429: {
            "severity": "clear",
            "summary": "Frei",
            "vehicles": 1,
            "trucks": 0,
            "wait_min": 0,
            "weather": "sunny",
            "road": "frei",
        },
    }

    def fake_analyze(client, image, api_key, model, name, direction):
        cam_id = 430 if "Ausreise" in name else 429
        return verdicts[cam_id]

    monkeypatch.setattr(hc, "_analyze_with_openai", fake_analyze)

    alerts = hc.fetch_hak_cameras(CONFIG)
    assert len(alerts) == 2
    by_id = {a.extras["cam_id"]: a for a in alerts}
    assert by_id[430].severity == "critical"
    assert by_id[430].delay_min == 80
    assert by_id[430].extras["image_url"] == "https://m.hak.hr/cam.asp?id=430"
    assert by_id[430].extras["role"] == "to_bih"
    assert "gpt-4o-mini" in by_id[430].detail
    assert by_id[429].severity == "info"  # clear → info
    assert by_id[429].extras["vehicles"] == 1
    assert by_id[429].extras["role"] == "to_hr"

    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("should use cache")

    monkeypatch.setattr(hc, "_analyze_with_openai", boom)
    cached = hc.fetch_hak_cameras(CONFIG)
    assert len(cached) == 2
    assert calls["n"] == 0


def test_maljevac_cam_wait_prefers_outbound():
    alerts = [
        Alert(
            source="HAK-Cam",
            severity="warning",
            title="Kamera: Maljevac — Einreise BiH → HR",
            detail="x",
            location="Maljevac — Einreise BiH → HR",
            delay_min=10,
            extras={"direction": "BiH->HR"},
        ),
        Alert(
            source="HAK-Cam",
            severity="critical",
            title="Kamera: Maljevac — Ausreise HR → BiH",
            detail="x",
            location="Maljevac — Ausreise HR → BiH",
            delay_min=55,
            extras={"direction": "HR->BiH", "relevant": True},
        ),
    ]
    assert hc.maljevac_cam_wait_min(alerts) == 55


def test_cameras_from_config():
    cams = hc.cameras_from_config(CONFIG)
    assert [c["id"] for c in cams] == [430, 429]
    assert cams[0]["relevant"] is True
    assert cams[0]["role"] == "to_bih"
    assert cams[1]["role"] == "to_hr"
    assert cams[0]["image_url"].endswith("id=430")


def test_dashboard_embeds_cameras():
    cam_bih = Alert(
        source="HAK-Cam",
        severity="critical",
        title="Kamera: Maljevac — Ausreise HR → BiH",
        detail="Lange Kolonne | KI: gpt-4o-mini",
        location="Maljevac — Ausreise HR → BiH",
        delay_min=80,
        extras={"vehicles": 22, "trucks": 2, "weather": "sunny", "road": "dicht", "role": "to_bih"},
    )
    cam_hr = Alert(
        source="HAK-Cam",
        severity="warning",
        title="Kamera: Maljevac — Einreise BiH → HR",
        detail="Mittlere Kolonne | KI: gpt-4o-mini",
        location="Maljevac — Einreise BiH → HR",
        delay_min=25,
        extras={"vehicles": 7, "trucks": 0, "weather": "sunny", "road": "flüssig", "role": "to_hr"},
    )
    payload = _payload_from_alerts(CONFIG, [cam_bih, cam_hr])
    assert [c["id"] for c in payload["cameras"]] == [430, 429]
    relevant = next(c for c in payload["cameras"] if c["id"] == 430)
    assert relevant["severity"] == "critical"
    assert relevant["wait_min"] == 80
    assert relevant["vehicles"] == 22
    assert relevant["role"] == "to_bih"

    mj = payload["maljevac_now"]
    assert mj is not None
    assert mj["source"].startswith("HAK-Cam")
    assert mj["to_bih"]["cars"] == 22
    assert mj["to_bih"]["wait_min"] == 80
    assert mj["to_hr"]["cars"] == 7
    assert mj["to_hr"]["wait_min"] == 25
    # Headline primary = direction to BiH
    assert mj["cars"] == 22

    html_out = render_html(payload)
    assert "Grenz-Kameras" in html_out
    assert "https://m.hak.hr/cam.asp?id=430" in html_out
    assert "Einfahrt BiH (HR → BiH)" in html_out
    assert "Einfahrt HR (BiH → HR)" in html_out
    assert "data-cam=" in html_out
    assert 'data-cam-open' in html_out
    assert 'id="cam-lightbox"' in html_out
    assert "Tippen · größer" in html_out
