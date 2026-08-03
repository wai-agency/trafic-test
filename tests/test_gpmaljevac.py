from traffic_monitor.sources.gpmaljevac import _clean, _looks_like_headline


def test_rejects_meta_junk():
    junk = 'tle" content="Granični prijelaz Maljevac" /><meta property="og:description"'
    assert not _looks_like_headline(_clean(junk))


def test_accepts_real_headline():
    title = "Kilometarske kolone širom Slovenije: Blokada pred tunelom Karavanke"
    assert _looks_like_headline(title)
