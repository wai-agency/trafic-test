from __future__ import annotations

from traffic_monitor.models import Alert
from traffic_monitor.notifiers import DEFAULT_DASHBOARD_URL, TelegramNotifier, dashboard_url


def test_dashboard_url_default(monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    assert dashboard_url() == DEFAULT_DASHBOARD_URL


def test_dashboard_url_override(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://example.com/dash")
    assert dashboard_url() == "https://example.com/dash/"


def test_telegram_appends_dashboard_link(monkeypatch):
    sent = {}

    class FakeResp:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            sent["url"] = url
            sent["json"] = json
            return FakeResp()

    import traffic_monitor.notifiers as n

    monkeypatch.setattr(n.httpx, "Client", FakeClient)
    monkeypatch.delenv("DASHBOARD_URL", raising=False)

    alert = Alert(
        source="HAK-Cam",
        severity="warning",
        title="Kamera: Maljevac",
        detail="~10 Autos",
        location="Maljevac",
        url="https://www.hak.hr/info/kamere/430.jpg",
    )
    TelegramNotifier("tok", "123").send(alert)
    text = sent["json"]["text"]
    assert "Dashboard: https://wai-agency.github.io/trafic-test/" in text
    assert "[WARNING] HAK-Cam: Kamera: Maljevac" in text


def test_telegram_does_not_duplicate_dashboard(monkeypatch):
    sent = {}

    class FakeResp:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            sent["json"] = json
            return FakeResp()

    import traffic_monitor.notifiers as n

    monkeypatch.setattr(n.httpx, "Client", FakeClient)

    dash = DEFAULT_DASHBOARD_URL
    alert = Alert(
        source="Test",
        severity="info",
        title="Ping",
        detail=f"ok\n\nDashboard: {dash}",
    )
    TelegramNotifier("tok", "123").send(alert)
    assert sent["json"]["text"].count("Dashboard:") == 1
