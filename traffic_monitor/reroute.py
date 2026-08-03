from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from traffic_monitor.models import Alert

# Lat,Lon for Google Maps waypoints
STUTTGART = (48.7758, 9.1829)
MUENCHEN = (48.1351, 11.5820)
SALZBURG = (47.8095, 13.0550)
VILLACH = (46.6103, 13.8558)
GRAZ = (47.0707, 15.4395)
MARIBOR = (46.5547, 15.6459)
ZAGREB = (45.8150, 15.9819)
MALJEVAC = (45.2005, 15.7875)
IZACIC = (44.8761, 15.7922)
BUZIM = (45.0613, 16.0324)


@dataclass(frozen=True, slots=True)
class RouteOption:
    id: str
    title: str
    summary: str
    points: tuple[tuple[float, float], ...]
    labels: tuple[str, ...]

    def google_maps_url(self) -> str:
        # Google Maps directions with coordinates
        path = "/".join(f"{lat},{lon}" for lat, lon in self.points)
        return f"https://www.google.com/maps/dir/{path}"

    def google_maps_search_url(self) -> str:
        # Named waypoints (often nicer in the app)
        names = "/".join(quote(label) for label in self.labels)
        return f"https://www.google.com/maps/dir/{names}"


ROUTES: dict[str, RouteOption] = {
    "primary": RouteOption(
        id="primary",
        title="Hauptroute (Karawanken + Maljevac)",
        summary="A8 → Salzburg → Tauern → Karawanken → Zagreb → Maljevac → Bužim",
        points=(STUTTGART, SALZBURG, VILLACH, ZAGREB, MALJEVAC, BUZIM),
        labels=("Stuttgart", "Salzburg", "Villach", "Zagreb", "Maljevac", "Bužim"),
    ),
    "via_graz": RouteOption(
        id="via_graz",
        title="Umleitung ohne Karawanken (Graz/Maribor)",
        summary="A8 → München → Graz → Maribor (Šentilj) → Zagreb → Maljevac → Bužim",
        points=(STUTTGART, MUENCHEN, GRAZ, MARIBOR, ZAGREB, MALJEVAC, BUZIM),
        labels=("Stuttgart", "München", "Graz", "Maribor", "Zagreb", "Maljevac", "Bužim"),
    ),
    "border_izacic": RouteOption(
        id="border_izacic",
        title="Gleiche Autobahn, Grenze Izačić",
        summary="Karawanken-Korridor, Einreise über Izačić statt Maljevac",
        points=(STUTTGART, SALZBURG, VILLACH, ZAGREB, IZACIC, BUZIM),
        labels=("Stuttgart", "Salzburg", "Villach", "Zagreb", "Izačić", "Bužim"),
    ),
    "via_graz_izacic": RouteOption(
        id="via_graz_izacic",
        title="Ohne Karawanken + Grenze Izačić",
        summary="Graz/Maribor und Einreise Izačić (bei Bihać)",
        points=(STUTTGART, MUENCHEN, GRAZ, MARIBOR, ZAGREB, IZACIC, BUZIM),
        labels=("Stuttgart", "München", "Graz", "Maribor", "Zagreb", "Izačić", "Bužim"),
    ),
}


def _alert_text(alert: Alert) -> str:
    return f"{alert.title} {alert.detail} {alert.location} {alert.source}".lower()


def detect_blockers(alerts: list[Alert]) -> set[str]:
    """Return blocker tags: karawanken, tauern, maljevac, izacic, a8."""
    blockers: set[str] = set()
    for alert in alerts:
        if alert.severity not in {"warning", "critical"}:
            continue
        if alert.title == "Quelle nicht erreichbar":
            continue
        text = _alert_text(alert)
        # Border waits from Nakordoni are especially actionable
        heavy_border = alert.source.lower() == "nakordoni" and (alert.delay_min or 0) >= 45
        critical = alert.severity == "critical" or heavy_border

        if any(k in text for k in ("karawan", "karavanke", "a11")) and critical:
            blockers.add("karawanken")
        if any(k in text for k in ("tauern", "katschberg", "a10")) and critical:
            blockers.add("tauern")
        if "maljevac" in text and (critical or heavy_border):
            blockers.add("maljevac")
        if any(k in text for k in ("izači", "izaci", "petrovo selo", "petrowo selo", "ličko", "licko", "litscho")):
            if critical or heavy_border:
                blockers.add("izacic")
        if alert.source == "autobahn.de" and alert.severity == "critical":
            blockers.add("a8")
    return blockers


def choose_reroutes(blockers: set[str]) -> list[RouteOption]:
    if not blockers:
        return []

    options: list[RouteOption] = []
    karawanken_blocked = "karawanken" in blockers or "tauern" in blockers
    maljevac_blocked = "maljevac" in blockers
    izacic_blocked = "izacic" in blockers

    if karawanken_blocked and maljevac_blocked and not izacic_blocked:
        options.append(ROUTES["via_graz_izacic"])
    elif karawanken_blocked and not maljevac_blocked:
        options.append(ROUTES["via_graz"])
    elif maljevac_blocked and not izacic_blocked and not karawanken_blocked:
        options.append(ROUTES["border_izacic"])
    elif maljevac_blocked and izacic_blocked and karawanken_blocked:
        # Both borders bad + tunnel: still suggest Graz + whichever border is less mentioned
        options.append(ROUTES["via_graz_izacic"])
        options.append(ROUTES["via_graz"])
    elif maljevac_blocked and izacic_blocked:
        options.append(ROUTES["via_graz"])
    elif "a8" in blockers and not karawanken_blocked:
        # Soft tip: keep primary but mention checking departure time
        options.append(ROUTES["primary"])

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[RouteOption] = []
    for route in options:
        if route.id in seen:
            continue
        seen.add(route.id)
        unique.append(route)
    return unique


def build_reroute_alerts(alerts: list[Alert]) -> list[Alert]:
    blockers = detect_blockers(alerts)
    routes = choose_reroutes(blockers)
    if not routes:
        return []

    reason = ", ".join(sorted(blockers))
    out: list[Alert] = []
    for route in routes:
        maps = route.google_maps_url()
        maps_named = route.google_maps_search_url()
        detail = (
            f"Grund: {reason}\n"
            f"{route.summary}\n"
            f"Google Maps: {maps}\n"
            f"Google Maps (Namen): {maps_named}"
        )
        out.append(
            Alert(
                source="Reroute",
                severity="critical",
                title=f"🧭 Alternative: {route.title}",
                detail=detail,
                location="Stuttgart → Bužim",
                url=maps,
                event_id=f"reroute:{route.id}:{reason}",
            )
        )
    return out
