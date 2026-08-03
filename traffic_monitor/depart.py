from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from traffic_monitor.config import env, load_config
from traffic_monitor.live_routing import (
    LiveRouteResult,
    border_wait_for_route,
    score_with_google,
    score_with_google_routes_api,
    score_with_osrm,
    score_with_tomtom,
)
from traffic_monitor.models import Alert
from traffic_monitor.notifiers import build_notifiers
from traffic_monitor.reroute import ROUTES, RouteOption
from traffic_monitor.sources.autobahn import fetch_autobahn
from traffic_monitor.sources.nakordoni import fetch_nakordoni

console = Console()
TZ = ZoneInfo("Europe/Berlin")

# Waiblingen (Nominatim)
WAIBLINGEN = (48.8325659, 9.3163822)


def with_origin(route: RouteOption, origin: tuple[float, float], origin_label: str) -> RouteOption:
    return RouteOption(
        id=route.id,
        title=route.title,
        summary=route.summary.replace("A8 →", f"Waiblingen → A8 →", 1)
        if route.summary.startswith("A8")
        else f"Ab {origin_label}: {route.summary}",
        points=(origin, *route.points[1:]),
        labels=(origin_label, *route.labels[1:]),
    )


def score_route_now(route: RouteOption, alerts: list[Alert]) -> LiveRouteResult | None:
    google_key = env("GOOGLE_MAPS_API_KEY")
    tomtom_key = env("TOMTOM_API_KEY")
    result = None
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


def google_depart_at(route: RouteOption, depart_at: datetime, api_key: str) -> LiveRouteResult | None:
    """Traffic-aware ETA for a future departure via Directions API."""
    origin = f"{route.points[0][0]},{route.points[0][1]}"
    destination = f"{route.points[-1][0]},{route.points[-1][1]}"
    mid = route.points[1:-1]
    params = {
        "origin": origin,
        "destination": destination,
        "mode": "driving",
        "departure_time": int(depart_at.timestamp()),
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
    duration = 0
    distance = 0
    for leg in data["routes"][0].get("legs") or []:
        traffic = (leg.get("duration_in_traffic") or {}).get("value")
        normal = (leg.get("duration") or {}).get("value") or 0
        duration += int(traffic if traffic is not None else normal)
        distance += int((leg.get("distance") or {}).get("value") or 0)
    return LiveRouteResult(
        route=route,
        duration_sec=duration,
        distance_m=distance,
        provider="Google Directions (Live/Prognose)",
        summary_via=route.summary,
    )


def advise_today(
    *,
    notify: bool = False,
    console_only: bool = False,
) -> str:
    now = datetime.now(TZ)
    config = load_config()
    alerts = []
    try:
        alerts.extend(fetch_nakordoni(config))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Nakordoni: {exc}[/yellow]")
    try:
        alerts.extend(fetch_autobahn(config))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Autobahn: {exc}[/yellow]")

    origin_label = "Waiblingen, Germany"
    candidates = [
        with_origin(ROUTES["primary"], WAIBLINGEN, origin_label),
        with_origin(ROUTES["via_graz"], WAIBLINGEN, origin_label),
        with_origin(ROUTES["border_izacic"], WAIBLINGEN, origin_label),
        with_origin(ROUTES["via_graz_izacic"], WAIBLINGEN, origin_label),
    ]

    # Current scores
    now_scores: list[LiveRouteResult] = []
    for route in candidates:
        scored = score_route_now(route, alerts)
        if scored:
            now_scores.append(scored)
    now_scores.sort(key=lambda r: r.total_sec)

    google_key = env("GOOGLE_MAPS_API_KEY")
    slots = []
    # Departure slots for rest of today + early tomorrow
    for hour, minute in (
        (now.hour + (1 if now.minute > 10 else 0), 0 if now.minute > 10 else ((now.minute // 15) + 1) * 15),
        (14, 0),
        (16, 0),
        (18, 0),
        (20, 0),
        (21, 30),
        (23, 0),
    ):
        if hour >= 24:
            continue
        dt = now.replace(hour=min(hour, 23), minute=0 if hour != now.hour else min(minute, 59), second=0, microsecond=0)
        if dt <= now:
            dt = now + timedelta(minutes=20)
        if dt.date() != now.date() and dt.hour not in (3, 4):
            continue
        slots.append(dt)
    # Always include tonight late and tomorrow early
    tomorrow = (now + timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
    tonight = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if tonight <= now:
        tonight = now + timedelta(minutes=30)
    for dt in (tonight, tomorrow, tomorrow.replace(hour=4, minute=30)):
        if dt not in slots:
            slots.append(dt)
    slots = sorted(set(slots))

    # Pick top 2 route shapes, compare across slots with Google if available
    top_shapes = now_scores[:2] if now_scores else []
    slot_rows = []
    if google_key and top_shapes:
        for dt in slots:
            for base in top_shapes:
                live = google_depart_at(base.route, dt, google_key)
                if not live:
                    continue
                live.border_wait_min = border_wait_for_route(base.route, alerts)
                arrive = dt + timedelta(seconds=live.total_sec)
                slot_rows.append((arrive, dt, live))
        slot_rows.sort(key=lambda x: x[0])  # earliest arrival wins
    elif now_scores:
        best = now_scores[0]
        # Heuristic: leave ASAP tonight OR very early tomorrow for less traffic
        for dt in slots:
            # Assume evening/night saves ~45-90 min vs afternoon peak
            penalty = 0
            if 15 <= dt.hour <= 19:
                penalty = 50 * 60
            elif 7 <= dt.hour <= 9:
                penalty = 25 * 60
            elif dt.hour >= 21 or dt.hour <= 5:
                penalty = -40 * 60
            total = best.total_sec + penalty
            arrive = dt + timedelta(seconds=total)
            slot_rows.append(
                (
                    arrive,
                    dt,
                    LiveRouteResult(
                        route=best.route,
                        duration_sec=total,
                        distance_m=best.distance_m,
                        provider=best.provider + " + Zeit-Heuristik",
                        border_wait_min=best.border_wait_min,
                        summary_via=best.summary_via,
                    ),
                )
            )
        slot_rows.sort(key=lambda x: x[0])

    lines = [
        f"Stand: {now.strftime('%a %d.%m.%Y %H:%M')} Europe/Berlin",
        "Von: Waiblingen → Bužim",
        "",
    ]

    # Border snapshot
    border_alerts = [a for a in alerts if a.source.lower() == "nakordoni"]
    if border_alerts:
        lines.append("Grenze HR→BA (live):")
        for a in sorted(border_alerts, key=lambda x: x.delay_min or 9999):
            lines.append(f"- {a.location or a.title}: ~{a.delay_min} min")
        lines.append("")

    if now_scores:
        lines.append("Routen-Vergleich (jetzt):")
        for i, r in enumerate(now_scores, 1):
            lines.append(
                f"{i}. {r.route.title}: {r.format_duration()} "
                f"(~{r.distance_m/1000:.0f} km, Grenze ~{r.border_wait_min} min) [{r.provider}]"
            )
        lines.append("")
        best_now = now_scores[0]
        lines.append(f"Beste Route jetzt: {best_now.route.title}")
        lines.append(best_now.route.summary)
        lines.append(f"Maps: {best_now.route.google_maps_url()}")
        lines.append("")

    if slot_rows:
        best_arrive, best_dep, best_live = slot_rows[0]
        lines.append("Beste Abfahrt für früheste Ankunft:")
        lines.append(f"→ Los: {best_dep.strftime('%a %d.%m. %H:%M')}")
        lines.append(f"→ Ankunft ca.: {best_arrive.strftime('%a %d.%m. %H:%M')}")
        lines.append(f"→ Route: {best_live.route.title} ({best_live.format_duration()})")
        lines.append(f"→ Maps: {best_live.route.google_maps_url()}")
        lines.append("")
        lines.append("Weitere gute Slots:")
        seen = set()
        for arrive, dep, live in slot_rows[:6]:
            key = (dep.strftime("%H:%M"), live.route.id)
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"- Los {dep.strftime('%d.%m. %H:%M')} → an ~{arrive.strftime('%d.%m. %H:%M')} "
                f"| {live.route.title} | {live.format_duration()}"
            )
    else:
        lines.append("Keine Slot-Berechnung möglich (Routing/Keys prüfen).")

    text = "\n".join(lines)
    console.print(Panel(text, title="Abfahrt Waiblingen → Bužim", border_style="green"))

    if notify:
        from traffic_monitor.models import Alert

        msg = Alert(
            source="DepartAdvisor",
            severity="critical",
            title="🚗 Beste Abfahrt heute Waiblingen → Bužim",
            detail=text,
            location="Waiblingen → Bužim",
            url=now_scores[0].route.google_maps_url() if now_scores else "",
            event_id=f"depart:{now.strftime('%Y%m%d%H')}",
        )
        build_notifiers(console_only=console_only).send(msg)
    return text
