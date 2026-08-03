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


def test_candidates_prefer_izacic_when_maljevac_blocked():
    routes = candidate_routes({"maljevac"})
    assert routes[0].id == "border_izacic"
