from __future__ import annotations

from traffic_monitor.sources import gpmaljevac as gpm
from traffic_monitor.sources.facebook_gpmaljevac import fetch_facebook_gpmaljevac
from traffic_monitor.sources.gpmaljevac import _clean, _is_relevant, _looks_like_headline


def test_rejects_meta_junk():
    junk = 'tle" content="Granični prijelaz Maljevac" /><meta property="og:description"'
    assert not _looks_like_headline(_clean(junk))


def test_accepts_real_headline():
    title = "Kilometarske kolone širom Slovenije: Blokada pred tunelom Karavanke"
    assert _looks_like_headline(title)


def test_relevant_border_queue_post():
    assert _is_relevant("haos na izlazu i ulazu u bih: duge kolone na maljevac i izačić")
    assert _is_relevant("saobraćajni kolaps u sloveniji: kilometarske kolone pred karavankama")
    assert _is_relevant(
        "na više graničnih prijelaza formirane duge kolone, najveće gužve na izačiću"
    )


def test_noise_filtered():
    assert not _is_relevant("lidl pokreće najveći konkurs u bih: traži se više od 1000 radnika")
    assert not _is_relevant("ubistvo u sarajevu: mladić ubijen nožem")
    assert not _is_relevant(
        "tragedija na mrežnici: utopio se 65-godišnji muškarac kod duge rese"
    )
    assert not _is_relevant(
        "suze u cazinu novi dom. the post appeared first on | gpmaljevac.com - granični prijelaz"
    )


def test_facebook_source_skips_without_token(monkeypatch):
    monkeypatch.delenv("FACEBOOK_PAGE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("FACEBOOK_ACCESS_TOKEN", raising=False)
    assert fetch_facebook_gpmaljevac({}) == []


def test_facebook_source_parses_posts(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-token")

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {
                        "id": "1",
                        "message": "Maljevac gužva — kolona oko 30 minuta prema BiH",
                        "permalink_url": "https://www.facebook.com/GPMaljevac/posts/1",
                        "created_time": "2026-08-03T10:00:00+0000",
                    },
                    {
                        "id": "2",
                        "message": "Lidl konkurs u BiH — 1000 radnika",
                        "permalink_url": "https://www.facebook.com/GPMaljevac/posts/2",
                    },
                ]
            }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return FakeResp()

    import traffic_monitor.sources.facebook_gpmaljevac as fb

    monkeypatch.setattr(fb.httpx, "Client", FakeClient)
    alerts = fetch_facebook_gpmaljevac({})
    assert len(alerts) == 1
    assert alerts[0].source == "Facebook GPMaljevac"
    assert alerts[0].severity == "critical"
    assert "Maljevac" in alerts[0].title


def test_rss_fetch_live():
    alerts = gpm.fetch_gpmaljevac({})
    # Live feed should yield at least corridor/traffic hits on busy days;
    # allow empty if feed is only lifestyle that day, but must not crash.
    assert isinstance(alerts, list)
    for a in alerts:
        assert a.source == "GPMaljevac"
        assert a.severity in {"warning", "critical"}
