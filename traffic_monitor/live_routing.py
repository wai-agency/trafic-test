from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import quote

import httpx

from traffic_monitor.config import env
from traffic_monitor.models import Alert
from traffic_monitor.reroute import ROUTES, RouteOption, detect_blockers


@dataclass(slots=True)
class LiveRouteResult:
    route: RouteOption
    duration_sec: int
    distance_m: int
    provider: str
    border_wait_min: int = 0
    summary_via: str = ""

    @property
    def total_sec(self) -> int:
        return self.duration_sec + self.border_wait_min * 60

    def maps_url(self) -> str:
        return self.route.google_maps_url()

    def format_duration(self) -> str:
        total = self.total_sec
        h, rem = divmod(total, 3600)
        m = rem // 60
        if h:
            return f"{h}h {m:02d}min"
        return f"{m} min"


def candidate_routes(blockers: set[str]) -> list[RouteOption]:
    """Always compare a live set of corridor options; bias by blockers."""
    ids = ["primary", "via_graz", "border_izacic", "via_graz_izacic"]
    if "maljevac" in blockers and "izacic" not in blockers:
        ids = ["border_izacic", "via_graz_izacic", "via_graz", "primary"]
    elif "karawanken" in blockers or "tauern" in blockers:
        ids = ["via_graz", "via_graz_izacic", "border_izacic", "primary"]
    elif "izacic" in blockers and "maljevac" not in blockers:
        ids = ["primary", "via_graz", "border_izacic", "via_graz_izacic"]
    return [ROUTES[i] for i in ids]


def border_wait_for_route(route: RouteOption, alerts: list[Alert]) -> int:
    """Attach live Nakordoni + HAK-Cam waits to the border used by this candidate."""
    from traffic_monitor.sources.hak_cameras import maljevac_cam_wait_min

    labels = " ".join(route.labels).lower() + " " + route.id
    waits: dict[str, int] = {}
    for alert in alerts:
        src = alert.source.lower()
        if src not in {"nakordoni", "hak-cam"} or alert.delay_min is None:
            continue
        text = f"{alert.title} {alert.location}".lower()
        if "maljevac" in text:
            waits["maljevac"] = max(waits.get("maljevac", 0), int(alert.delay_min))
        if any(k in text for k in ("izači", "izaci", "petrovo", "petrowo", "ličko", "licko", "litscho")):
            waits["izacic"] = max(waits.get("izacic", 0), int(alert.delay_min))

    cam_wait = maljevac_cam_wait_min(alerts)
    if cam_wait is not None:
        waits["maljevac"] = max(waits.get("maljevac", 0), cam_wait)

    if "izacic" in labels or "izačić" in labels:
        return waits.get("izacic", 0)
    if "maljevac" in labels:
        return waits.get("maljevac", 0)
    return 0


def score_with_osrm(route: RouteOption) -> LiveRouteResult | None:
    coord = ";".join(f"{lon},{lat}" for lat, lon in route.points)
    url = f"https://router.project-osrm.org/route/v1/driving/{coord}?overview=false&steps=false"
    try:
        with httpx.Client(timeout=30.0, headers={"User-Agent": "stuttgart-buzim-traffic/1.0"}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        return None
    routes = data.get("routes") or []
    if not routes:
        return None
    best = routes[0]
    return LiveRouteResult(
        route=route,
        duration_sec=int(best["duration"]),
        distance_m=int(best["distance"]),
        provider="OSRM (ohne Live-Stau, + Grenzwartezeit)",
        summary_via=route.summary,
    )


def score_with_google(route: RouteOption, api_key: str) -> LiveRouteResult | None:
    """Google Directions legacy with live traffic (departure_time=now)."""
    origin = f"{route.points[0][0]},{route.points[0][1]}"
    destination = f"{route.points[-1][0]},{route.points[-1][1]}"
    mid = route.points[1:-1]
    params = {
        "origin": origin,
        "destination": destination,
        "mode": "driving",
        "departure_time": "now",
        "traffic_model": "best_guess",
        "key": api_key,
        "language": "de",
    }
    if mid:
        params["waypoints"] = "|".join(f"{lat},{lon}" for lat, lon in mid)
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get("https://maps.googleapis.com/maps/api/directions/json", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        return None
    if data.get("status") != "OK" or not data.get("routes"):
        return None
    legs = data["routes"][0].get("legs") or []
    duration = 0
    distance = 0
    for leg in legs:
        traffic = (leg.get("duration_in_traffic") or {}).get("value")
        normal = (leg.get("duration") or {}).get("value") or 0
        duration += int(traffic if traffic is not None else normal)
        distance += int((leg.get("distance") or {}).get("value") or 0)
    return LiveRouteResult(
        route=route,
        duration_sec=duration,
        distance_m=distance,
        provider="Google Directions (Live-Verkehr)",
        summary_via=route.summary,
    )


def score_with_google_routes_api(route: RouteOption, api_key: str) -> LiveRouteResult | None:
    """Google Routes API computeRoutes with TRAFFIC_AWARE_OPTIMAL."""
    body = {
        "origin": {"location": {"latLng": {"latitude": route.points[0][0], "longitude": route.points[0][1]}}},
        "destination": {
            "location": {"latLng": {"latitude": route.points[-1][0], "longitude": route.points[-1][1]}}
        },
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "departureTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "languageCode": "de",
    }
    intermediates = [
        {"location": {"latLng": {"latitude": lat, "longitude": lon}}} for lat, lon in route.points[1:-1]
    ]
    if intermediates:
        body["intermediates"] = intermediates
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                "https://routes.googleapis.com/directions/v2:computeRoutes",
                headers=headers,
                json=body,
            )
            if resp.status_code >= 400:
                return None
            data = resp.json()
    except httpx.HTTPError:
        return None
    routes = data.get("routes") or []
    if not routes:
        return None
    r0 = routes[0]
    dur_raw = r0.get("duration") or "0s"
    # duration like "1234s"
    duration_sec = int(str(dur_raw).rstrip("s") or 0)
    return LiveRouteResult(
        route=route,
        duration_sec=duration_sec,
        distance_m=int(r0.get("distanceMeters") or 0),
        provider="Google Routes (Live-Verkehr)",
        summary_via=route.summary,
    )


def score_with_tomtom(route: RouteOption, api_key: str) -> LiveRouteResult | None:
    path = ":".join(f"{lat},{lon}" for lat, lon in route.points)
    url = (
        f"https://api.tomtom.com/routing/1/calculateRoute/{path}/json"
        f"?traffic=true&travelMode=car&key={api_key}"
    )
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        return None
    routes = data.get("routes") or []
    if not routes:
        return None
    summary = routes[0].get("summary") or {}
    return LiveRouteResult(
        route=route,
        duration_sec=int(summary.get("travelTimeInSeconds") or 0),
        distance_m=int(summary.get("lengthInMeters") or 0),
        provider="TomTom (Live-Verkehr)",
        summary_via=route.summary,
    )


def score_route(route: RouteOption, alerts: list[Alert]) -> LiveRouteResult | None:
    google_key = env("GOOGLE_MAPS_API_KEY")
    tomtom_key = env("TOMTOM_API_KEY")
    result: LiveRouteResult | None = None
    if google_key:
        result = score_with_google_routes_api(route, google_key) or score_with_google(route, google_key)
    if result is None and tomtom_key:
        result = score_with_tomtom(route, tomtom_key)
    if result is None:
        result = score_with_osrm(route)
    if result is None:
        return None
    result.border_wait_min = border_wait_for_route(route, alerts)
    return result


def compute_live_best(alerts: list[Alert]) -> list[LiveRouteResult]:
    blockers = detect_blockers(alerts)
    if not blockers:
        return []
    scored: list[LiveRouteResult] = []
    for route in candidate_routes(blockers):
        result = score_route(route, alerts)
        if result and result.duration_sec > 0:
            scored.append(result)
    scored.sort(key=lambda r: r.total_sec)
    return scored


def build_live_reroute_alerts(alerts: list[Alert]) -> list[Alert]:
    ranked = compute_live_best(alerts)
    if not ranked:
        return []
    blockers = ", ".join(sorted(detect_blockers(alerts)))
    best = ranked[0]
    lines = [
        f"Live berechnet wegen: {blockers}",
        f"Provider: {best.provider}",
        f"Beste jetzt: {best.route.title}",
        f"Dauer inkl. Grenzwartezeit: {best.format_duration()}"
        + (f" (davon Grenze ~{best.border_wait_min} min)" if best.border_wait_min else ""),
        f"Distanz: ~{best.distance_m/1000:.0f} km",
        f"{best.summary_via}",
        f"Google Maps: {best.maps_url()}",
    ]
    if len(ranked) > 1:
        lines.append("")
        lines.append("Vergleich:")
        for item in ranked[:4]:
            mark = "← best" if item is best else ""
            lines.append(
                f"- {item.route.title}: {item.format_duration()} "
                f"(~{item.distance_m/1000:.0f} km) {mark}"
            )

    # Fingerprint by best route + rough duration bucket (30 min) so updates re-notify on big changes
    bucket = best.total_sec // 1800
    return [
        Alert(
            source="LiveRoute",
            severity="critical",
            title=f"🧭 Live-Route: {best.route.title} ({best.format_duration()})",
            detail="\n".join(lines),
            location="Stuttgart → Bužim",
            url=best.maps_url(),
            event_id=f"liveroute:{best.route.id}:{bucket}:{blockers}",
            delay_min=best.total_sec // 60,
        )
    ]
