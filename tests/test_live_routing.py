from traffic_monitor.live_routing import border_wait_for_route, candidate_routes
from traffic_monitor.models import Alert
from traffic_monitor.reroute import ROUTES


def test_border_wait_attached_to_maljevac_route():
    alerts = [
        Alert(
            source="Nakordoni",
            severity="critical",
            title="Grenze: Maljevac",
            detail="wait",
            location="Maljevac",
            delay_min=120,
        )
    ]
    wait = border_wait_for_route(ROUTES["primary"], alerts)
    assert wait == 120
    wait_iz = border_wait_for_route(ROUTES["border_izacic"], alerts)
    assert wait_iz == 0


def test_border_wait_adds_lucko_for_zagreb_routes():
    alerts = [
        Alert(
            source="HAK-Cam",
            severity="warning",
            title="Kamera: Maljevac — Einreise BiH → HR",
            detail="x",
            location="Maljevac — Einreise BiH → HR",
            delay_min=15,
            extras={"role": "to_hr"},
        ),
        Alert(
            source="HAK-Cam",
            severity="critical",
            title="Kamera: Lučko — Ulaz +1 km",
            detail="x",
            location="Lučko — Ulaz +1 km",
            delay_min=30,
            extras={"role": "lucko_entry"},
        ),
    ]
    wait = border_wait_for_route(ROUTES["primary"], alerts)
    assert wait == 45  # Maljevac 15 + Lučko 30


def test_candidates_prefer_izacic_when_maljevac_blocked():
    routes = candidate_routes({"maljevac"})
    assert routes[0].id == "border_izacic"
