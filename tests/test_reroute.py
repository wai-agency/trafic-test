from traffic_monitor.models import Alert
from traffic_monitor.reroute import build_reroute_alerts, choose_reroutes, detect_blockers


def test_maljevac_suggests_izacic():
    alerts = [
        Alert(
            source="Nakordoni",
            severity="critical",
            title="Grenze: Maljevac",
            detail="Wartezeit ~149 min",
            location="Maljevac",
            delay_min=149,
        )
    ]
    blockers = detect_blockers(alerts)
    assert "maljevac" in blockers
    routes = choose_reroutes(blockers)
    assert any(r.id == "border_izacic" for r in routes)
    msgs = build_reroute_alerts(alerts)
    assert msgs
    assert "google.com/maps/dir" in msgs[0].url


def test_karawanken_suggests_graz():
    alerts = [
        Alert(
            source="GPMaljevac",
            severity="critical",
            title="Blokada pred tunelom Karavanke",
            detail="kolone",
            location="Karawanken",
        )
    ]
    routes = choose_reroutes(detect_blockers(alerts))
    assert any(r.id == "via_graz" for r in routes)


def test_both_suggest_graz_izacic():
    alerts = [
        Alert(
            source="GPMaljevac",
            severity="critical",
            title="Karawanken Stau",
            detail="kolona",
            location="Karawanken",
        ),
        Alert(
            source="Nakordoni",
            severity="critical",
            title="Grenze: Maljevac",
            detail="wait",
            location="Maljevac",
            delay_min=120,
        ),
    ]
    routes = choose_reroutes(detect_blockers(alerts))
    assert any(r.id == "via_graz_izacic" for r in routes)
