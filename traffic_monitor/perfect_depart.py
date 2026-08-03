from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from traffic_monitor.config import env, load_config
from traffic_monitor.depart import WAIBLINGEN, with_origin
from traffic_monitor.models import Alert
from traffic_monitor.notifiers import build_notifiers
from traffic_monitor.reroute import ROUTES, RouteOption
from traffic_monitor.sources.nakordoni import fetch_nakordoni

console = Console()
TZ = ZoneInfo("Europe/Berlin")


@dataclass(slots=True)
class SlotScore:
    depart: datetime
    arrive_buzim: datetime
    arrive_border: datetime
    route: RouteOption
    drive_sec: int
    border_wait_min: int
    border_cars: float
    distance_m: int
    provider: str
    notes: str = ""

    @property
    def total_sec(self) -> int:
        return self.drive_sec + self.border_wait_min * 60

    def fmt_dur(self, sec: int | None = None) -> str:
        sec = self.total_sec if sec is None else sec
        h, rem = divmod(sec, 3600)
        m = rem // 60
        return f"{h}h {m:02d}min" if h else f"{m} min"


def _load_maljevac_forecast(api_key: str) -> list[dict]:
    url = "https://nakordoni.eu/api/v2/data/forecast?ppid=id_626"
    with httpx.Client(timeout=25.0, headers={"Authorization": f"Bearer {api_key}"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    return list(data.get("data") or [])


def _forecast_cars_at(forecast: list[dict], when: datetime) -> tuple[float, float, float]:
    """Return (cars, lower, upper) nearest forecast point."""
    if not forecast:
        return (8.0, 7.0, 9.0)
    target = when.timestamp()
    best = min(forecast, key=lambda r: abs(r["time"] - target))
    return (
        float(best.get("avg_cars") or 8),
        float(best.get("lower_bound") or best.get("avg_cars") or 7),
        float(best.get("upper_bound") or best.get("avg_cars") or 9),
    )


def _cars_to_wait_min(cars: float) -> int:
    # History often ~6 min/car; current spike ~11. Use conservative 8.
    return max(10, int(round(cars * 8)))


def google_leg_times(
    route: RouteOption,
    depart_at: datetime,
    api_key: str,
) -> tuple[int, int, int] | None:
    """
    Returns (drive_sec_to_border, drive_sec_total, distance_m)
    Border = second-to-last waypoint (Maljevac/Izačić).
    """
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
        with httpx.Client(timeout=40.0) as client:
            resp = client.get("https://maps.googleapis.com/maps/api/directions/json", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        return None
    if data.get("status") != "OK" or not data.get("routes"):
        return None
    legs = data["routes"][0].get("legs") or []
    if not legs:
        return None

    def leg_sec(leg: dict) -> int:
        traffic = (leg.get("duration_in_traffic") or {}).get("value")
        normal = (leg.get("duration") or {}).get("value") or 0
        return int(traffic if traffic is not None else normal)

    secs = [leg_sec(leg) for leg in legs]
    dist = sum(int((leg.get("distance") or {}).get("value") or 0) for leg in legs)
    total = sum(secs)
    # Border is end of penultimate leg (arrival at Maljevac/Izačić waypoint)
    to_border = sum(secs[:-1]) if len(secs) >= 2 else total
    return to_border, total, dist


def osrm_leg_times(route: RouteOption) -> tuple[int, int, int] | None:
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
    legs = routes[0].get("legs") or []
    secs = [int(leg["duration"]) for leg in legs]
    dist = int(routes[0]["distance"])
    total = sum(secs)
    to_border = sum(secs[:-1]) if len(secs) >= 2 else total
    return to_border, total, dist


def candidate_routes() -> list[RouteOption]:
    origin = "Waiblingen, Germany"
    return [
        with_origin(ROUTES["primary"], WAIBLINGEN, origin),
        with_origin(ROUTES["via_graz"], WAIBLINGEN, origin),
        with_origin(ROUTES["border_izacic"], WAIBLINGEN, origin),
        with_origin(ROUTES["via_graz_izacic"], WAIBLINGEN, origin),
    ]


def departure_slots(now: datetime) -> list[datetime]:
    slots: list[datetime] = []
    # rest of Monday afternoon/evening + night + early Tuesday
    day = now.date()
    for hour, minute in (
        (15, 0),
        (15, 30),
        (16, 0),
        (16, 30),
        (17, 0),
        (17, 30),
        (18, 0),
        (19, 0),
        (20, 0),
        (21, 0),
        (22, 0),
        (23, 0),
        (0, 0),  # Tuesday
        (1, 0),
        (2, 0),
        (3, 0),
        (4, 0),
    ):
        if hour >= 15:
            dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
        else:
            # early hours => next calendar day
            nxt = day + timedelta(days=1)
            dt = datetime(nxt.year, nxt.month, nxt.day, hour, minute, tzinfo=TZ)
        if dt <= now + timedelta(minutes=10):
            continue
        slots.append(dt)
    return slots


def score_slot(
    depart: datetime,
    route: RouteOption,
    forecast: list[dict],
    google_key: str | None,
) -> SlotScore | None:
    times = None
    provider = ""
    if google_key:
        times = google_leg_times(route, depart, google_key)
        provider = "Google Live-Verkehr"
    if times is None:
        times = osrm_leg_times(route)
        provider = "OSRM (ohne Straßenstau)"
    if times is None:
        return None
    to_border, drive_total, dist = times
    arrive_border = depart + timedelta(seconds=to_border)
    cars, lo, hi = _forecast_cars_at(forecast, arrive_border)
    # Only apply Maljevac forecast wait to Maljevac routes; for Izačić use live nakordoni if available later
    uses_maljevac = "maljevac" in " ".join(route.labels).lower()
    if uses_maljevac:
        border_wait = _cars_to_wait_min(cars)
        notes = f"Maljevac-Forecast ~{cars:.1f} Autos (Band {lo:.1f}-{hi:.1f}) → ~{border_wait} min"
    else:
        # Izačić: no separate forecast here; assume similar night pattern but +15% uncertainty
        border_wait = _cars_to_wait_min(cars * 1.05)
        notes = f"Izačić ohne eigenen Forecast; Schätzung analog ~{border_wait} min"
    arrive_buzim = depart + timedelta(seconds=drive_total + border_wait * 60)
    return SlotScore(
        depart=depart,
        arrive_buzim=arrive_buzim,
        arrive_border=arrive_border,
        route=route,
        drive_sec=drive_total,
        border_wait_min=border_wait,
        border_cars=cars,
        distance_m=dist,
        provider=provider,
        notes=notes,
    )


def optimize(
    *,
    notify: bool = False,
    console_only: bool = False,
) -> str:
    now = datetime.now(TZ)
    nk_key = env("NAKORDONI_API_KEY")
    google_key = env("GOOGLE_MAPS_API_KEY")
    if not nk_key:
        raise SystemExit("NAKORDONI_API_KEY fehlt")

    forecast = _load_maljevac_forecast(nk_key)
    routes = candidate_routes()
    slots = departure_slots(now)

    scored: list[SlotScore] = []
    for dep in slots:
        for route in routes:
            s = score_slot(dep, route, forecast, google_key)
            if s:
                scored.append(s)

    if not scored:
        raise SystemExit("Keine Scores berechnet")

    # Rank: earliest arrival at Bužim, tie-break lower border wait / lower border cars
    scored.sort(key=lambda s: (s.arrive_buzim, s.border_wait_min, s.border_cars, s.drive_sec))
    best = scored[0]

    # Also find best among "low border risk" (forecast cars <= 3.5)
    low_border = [s for s in scored if s.border_cars <= 3.5]
    best_low = min(low_border, key=lambda s: s.arrive_buzim) if low_border else None

    table = Table(title="Top Abfahrten Waiblingen → Bužim (komplett)")
    table.add_column("Los")
    table.add_column("An Grenze")
    table.add_column("Grenze")
    table.add_column("An Bužim")
    table.add_column("Route")
    table.add_column("Total")
    for s in scored[:12]:
        table.add_row(
            s.depart.strftime("%a %H:%M"),
            s.arrive_border.strftime("%a %H:%M"),
            f"~{s.border_wait_min}m/{s.border_cars:.1f}A",
            s.arrive_buzim.strftime("%a %H:%M"),
            s.route.id,
            s.fmt_dur(),
        )
    console.print(table)

    lines = [
        f"Stand: {now.strftime('%a %d.%m.%Y %H:%M')} Europe/Berlin",
        "Ziel: schnell ankommen + wenig Stau + kurze Grenze Maljevac/BiH",
        f"Routing: {best.provider}",
        "",
        "🏆 EMPFEHLUNG (früheste Ankunft Bužim):",
        f"Los: {best.depart.strftime('%a %d.%m. %H:%M')}",
        f"Route: {best.route.title}",
        f"{best.route.summary}",
        f"An Grenze ca.: {best.arrive_border.strftime('%a %d.%m. %H:%M')}",
        f"Grenzwartezeit ca.: {best.border_wait_min} min ({best.notes})",
        f"An Bužim ca.: {best.arrive_buzim.strftime('%a %d.%m. %H:%M')}",
        f"Gesamt: {best.fmt_dur()} | ~{best.distance_m/1000:.0f} km",
        f"Maps: {best.route.google_maps_url()}",
    ]
    if best_low and best_low.depart != best.depart:
        lines += [
            "",
            "🌙 Beste Variante mit freier Grenze (Forecast ≤ ~3.5 Autos):",
            f"Los: {best_low.depart.strftime('%a %d.%m. %H:%M')}",
            f"Route: {best_low.route.title}",
            f"An Grenze ca.: {best_low.arrive_border.strftime('%a %d.%m. %H:%M')}",
            f"Grenze ca.: {best_low.border_wait_min} min / {best_low.border_cars:.1f} Autos",
            f"An Bužim ca.: {best_low.arrive_buzim.strftime('%a %d.%m. %H:%M')}",
            f"Maps: {best_low.route.google_maps_url()}",
        ]

    lines += [
        "",
        "Hinweis: Google bewertet Straßenstau live/prognostisch; Grenze kommt von Nakordoni-Forecast Maljevac.",
    ]
    text = "\n".join(lines)
    console.print(Panel(text, title="Perfekte Abfahrt", border_style="green"))

    if notify:
        msg = Alert(
            source="PerfectDepart",
            severity="critical",
            title=f"🚗 Perfekte Abfahrt: {best.depart.strftime('%a %H:%M')} → Bužim ~{best.arrive_buzim.strftime('%H:%M')}",
            detail=text,
            location="Waiblingen → Bužim",
            url=best.route.google_maps_url(),
            event_id=f"perfect:{best.depart.strftime('%Y%m%d%H%M')}:{best.route.id}",
        )
        build_notifiers(console_only=console_only).send(msg)
    return text
