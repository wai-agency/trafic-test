from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from traffic_monitor.config import env
from traffic_monitor.models import Alert
from traffic_monitor.notifiers import build_notifiers
from traffic_monitor.reroute import ROUTES, RouteOption

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
    lucko_wait_min: int = 0

    @property
    def total_sec(self) -> int:
        return self.drive_sec + (self.border_wait_min + self.lucko_wait_min) * 60

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


def _border_waypoint_index(route: RouteOption) -> int:
    """Index of Maljevac/Izačić in route.points (arrival = sum of legs[:index])."""
    for i, label in enumerate(route.labels):
        low = label.lower()
        if "maljevac" in low or "izači" in low or "izaci" in low:
            return i
    # Fallback: early border on return trips, late border on outbound templates
    return 1 if len(route.points) > 2 else max(len(route.points) - 2, 0)


def _drive_to_border(secs: list[int], route: RouteOption) -> int:
    idx = _border_waypoint_index(route)
    if not secs:
        return 0
    if idx <= 0:
        return 0
    return sum(secs[: min(idx, len(secs))])


def google_leg_times(
    route: RouteOption,
    depart_at: datetime,
    api_key: str,
) -> tuple[int, int, int] | None:
    """
    Returns (drive_sec_to_border, drive_sec_total, distance_m)
    Border = Maljevac/Izačić waypoint (early on Bužim→Waiblingen return).
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
    return _drive_to_border(secs, route), total, dist


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
    return _drive_to_border(secs, route), total, dist


def candidate_routes() -> list[RouteOption]:
    return [
        ROUTES["primary"],
        ROUTES["via_graz"],
        ROUTES["border_izacic"],
        ROUTES["via_graz_izacic"],
    ]


def departure_slots(now: datetime) -> list[datetime]:
    """Sample upcoming departures for ~36h, with night slots 00–05 always included.

    Density (API-friendly):
    - 13:00–23:00 → every 30 min
    - 00:00–05:00 → every hour (00, 01, 02, 03, 04, 05)
    - other daytime hours → every hour
    """
    slots: list[datetime] = []
    # next half-hour boundary at least 20 min out
    cursor = now.replace(second=0, microsecond=0) + timedelta(minutes=20)
    if cursor.minute == 0:
        pass
    elif cursor.minute <= 30:
        cursor = cursor.replace(minute=30)
    else:
        cursor = (cursor + timedelta(hours=1)).replace(minute=0)

    end = now + timedelta(hours=36)
    t = cursor
    while t <= end:
        hour = t.hour
        if 13 <= hour <= 23:
            slots.append(t)
            t += timedelta(minutes=30)
        elif hour <= 5:
            # night window: only :00
            if t.minute == 0:
                slots.append(t)
            t = (t + timedelta(hours=1)).replace(minute=0)
        else:
            if t.minute == 0:
                slots.append(t)
            t = (t + timedelta(hours=1)).replace(minute=0)

    # Guarantee tonight/tomorrow 00–05 even if sampling edge-cases skip them
    for day_offset in (0, 1):
        day = (now + timedelta(days=day_offset)).date()
        for hour in (0, 1, 2, 3, 4, 5):
            dt = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
            if dt > now + timedelta(minutes=20):
                slots.append(dt)

    uniq = sorted({s.replace(second=0, microsecond=0) for s in slots})
    return [s for s in uniq if now + timedelta(minutes=15) < s <= end]


def score_slot(
    depart: datetime,
    route: RouteOption,
    forecast: list[dict],
    google_key: str | None,
    *,
    live_cam_wait_min: int | None = None,
    live_lucko_wait_min: int | None = None,
    now: datetime | None = None,
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
    ref = now or datetime.now(TZ)
    # Only apply Maljevac forecast wait to Maljevac routes; for Izačić use live nakordoni if available later
    uses_maljevac = "maljevac" in " ".join(route.labels).lower()
    if uses_maljevac:
        border_wait = _cars_to_wait_min(cars)
        notes = f"Maljevac-Forecast ~{cars:.1f} Autos (Band {lo:.1f}-{hi:.1f}) → ~{border_wait} min"
        # If border arrival is soon, blend in live HAK camera KI wait
        hours_to_border = (arrive_border - ref).total_seconds() / 3600
        if live_cam_wait_min is not None and hours_to_border <= 3:
            blended = max(border_wait, int(live_cam_wait_min))
            if blended != border_wait:
                notes += f" · HAK-Cam-KI live ~{live_cam_wait_min} min → genutzt {blended} min"
                border_wait = blended
    else:
        # Izačić: no separate forecast here; assume similar night pattern but +15% uncertainty
        border_wait = _cars_to_wait_min(cars * 1.05)
        notes = f"Izačić ohne eigenen Forecast; Schätzung analog ~{border_wait} min"

    # Lučko toll after Maljevac (~1.5h): use live wait to avoid Zagreb approach jam
    lucko_wait = 0
    if live_lucko_wait_min is not None and live_lucko_wait_min > 0:
        arrive_lucko = arrive_border + timedelta(minutes=90)
        hours_to_lucko = (arrive_lucko - ref).total_seconds() / 3600
        if hours_to_lucko <= 5:
            lucko_wait = int(live_lucko_wait_min)
            notes += f" · Lučko live ~{lucko_wait} min (Stau vermeiden)"

    arrive_buzim = depart + timedelta(
        seconds=drive_total + (border_wait + lucko_wait) * 60
    )
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
        lucko_wait_min=lucko_wait,
    )


def _slot_dict(s: SlotScore, *, badge: str | None = None) -> dict:
    return {
        "badge": badge,
        "depart": s.depart.isoformat(),
        "depart_label": s.depart.strftime("%a %d.%m. %H:%M"),
        "depart_short": s.depart.strftime("%H:%M"),
        "depart_day": s.depart.strftime("%a %d.%m."),
        "arrive_border": s.arrive_border.isoformat(),
        "arrive_border_label": s.arrive_border.strftime("%a %d.%m. %H:%M"),
        "arrive_border_short": s.arrive_border.strftime("%H:%M"),
        "arrive_buzim": s.arrive_buzim.isoformat(),
        "arrive_buzim_label": s.arrive_buzim.strftime("%a %d.%m. %H:%M"),
        "arrive_buzim_short": s.arrive_buzim.strftime("%H:%M"),
        "border_wait_min": s.border_wait_min,
        "border_cars": round(s.border_cars, 1),
        "lucko_wait_min": s.lucko_wait_min,
        "drive_min": s.drive_sec // 60,
        "total_min": s.total_sec // 60,
        "total_label": s.fmt_dur(),
        "distance_km": round(s.distance_m / 1000),
        "route_id": s.route.id,
        "route_title": s.route.title,
        "route_summary": s.route.summary,
        "maps_url": s.route.google_maps_url(),
        "provider": s.provider,
        "notes": s.notes,
        "stops": list(s.route.labels),
    }


def compute_perfect_payload() -> dict:
    now = datetime.now(TZ)
    nk_key = env("NAKORDONI_API_KEY")
    google_key = env("GOOGLE_MAPS_API_KEY")
    if not nk_key:
        raise SystemExit("NAKORDONI_API_KEY fehlt")

    forecast = _load_maljevac_forecast(nk_key)
    routes = candidate_routes()
    primary_route = next(r for r in routes if r.id == "primary")
    alt_routes = [r for r in routes if r.id != "primary"]
    slots = departure_slots(now)

    live_cam_wait = None
    live_lucko_wait = None
    try:
        from traffic_monitor.config import load_config
        from traffic_monitor.sources.hak_cameras import (
            fetch_hak_cameras,
            lucko_cam_wait_min,
            maljevac_cam_wait_min,
        )

        cam_alerts = fetch_hak_cameras(load_config())
        live_cam_wait = maljevac_cam_wait_min(cam_alerts)
        live_lucko_wait = lucko_cam_wait_min(cam_alerts)
    except Exception:
        live_cam_wait = None
        live_lucko_wait = None

    def _score(dep: datetime, route: RouteOption, *, use_google: bool) -> SlotScore | None:
        return score_slot(
            dep,
            route,
            forecast,
            google_key if use_google else None,
            live_cam_wait_min=live_cam_wait,
            live_lucko_wait_min=live_lucko_wait,
            now=now,
        )

    # Pass 1: all slots × Hauptroute with Google (timeline + ranking base)
    # Keeps Directions quota sane vs scoring 4 routes for every slot.
    scored: list[SlotScore] = []
    for dep in slots:
        s = _score(dep, primary_route, use_google=True)
        if s:
            scored.append(s)

    if not scored:
        raise SystemExit("Keine Scores berechnet")

    scored.sort(key=lambda s: (s.arrive_buzim, s.border_wait_min, s.border_cars, s.drive_sec))
    # Pass 2: compare alternate routes only on the most promising departures
    top_deps = []
    seen: set[str] = set()
    for s in scored:
        key = s.depart.isoformat()
        if key in seen:
            continue
        seen.add(key)
        top_deps.append(s.depart)
        if len(top_deps) >= 8:
            break
    for dep in top_deps:
        for route in alt_routes:
            s = _score(dep, route, use_google=True)
            if s:
                scored.append(s)

    scored.sort(key=lambda s: (s.arrive_buzim, s.border_wait_min, s.border_cars, s.drive_sec))
    best = scored[0]
    low_border = [s for s in scored if s.border_cars <= 3.5]
    best_low = min(low_border, key=lambda s: s.arrive_buzim) if low_border else None

    # Unique departure times for primary route (timeline strip)
    primary = [s for s in scored if s.route.id == "primary"]
    primary.sort(key=lambda s: s.depart)
    timeline = []
    for s in primary:
        # ~6 Autos noch ok (orange); rot erst darüber bzw. wirklich voll.
        load = "frei" if s.border_cars <= 3.0 else ("ok" if s.border_cars <= 6.0 else "voll")
        night = s.depart.hour <= 5
        timeline.append(
            {
                "depart_short": s.depart.strftime("%H:%M"),
                "depart_day": s.depart.strftime("%a"),
                "depart": s.depart.isoformat(),
                "arrive_buzim_short": s.arrive_buzim.strftime("%H:%M"),
                "arrive_border_short": s.arrive_border.strftime("%H:%M"),
                "border_wait_min": s.border_wait_min,
                "border_cars": round(s.border_cars, 1),
                "total_label": s.fmt_dur(),
                "load": load,
                "night": night,
                "is_best": s.depart == best.depart and s.route.id == best.route.id,
            }
        )

    return {
        "generated_at": now.isoformat(),
        "generated_label": now.strftime("%d.%m.%Y %H:%M"),
        "refresh_minutes": 30,
        "origin": "Bužim",
        "destination": "Waiblingen",
        "provider": best.provider,
        "slot_count": len(primary),
        "best": _slot_dict(best, badge="früheste Ankunft"),
        "best_low_border": (
            _slot_dict(best_low, badge="freie Grenze")
            if best_low and best_low.depart != best.depart
            else None
        ),
        "top": [_slot_dict(s) for s in scored[:10]],
        "timeline": timeline,
        "hint": (
            "Rückfahrt Bužim → Waiblingen · Google Live-Stau + Nakordoni Maljevac-Forecast "
            "(+ HAK-Cam KI wenn Grenze nah). Abfahrtsfenster inkl. 00–05. "
            "Verpasste Abfahrt → automatisch nächste beste."
        ),
    }


def write_perfect_json(path: str | Path, payload: dict | None = None) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = payload if payload is not None else compute_perfect_payload()
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_perfect_json(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return advance_perfect_payload(data)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def advance_perfect_payload(
    payload: dict | None,
    now: datetime | None = None,
    *,
    grace_min: int = 5,
) -> dict | None:
    """Drop past departures and promote the next-best future slot.

    Used when perfect.json is cached (~30 min) so a missed Abfahrt does not
    stay on the dashboard until the next Google refresh.
    """
    if not payload or not payload.get("best"):
        return payload

    now = now or datetime.now(TZ)
    cutoff = now - timedelta(minutes=grace_min)

    def is_upcoming(slot: dict | None) -> bool:
        if not slot:
            return False
        dep = _parse_iso(slot.get("depart"))
        return dep is not None and dep > cutoff

    def sort_key(s: dict):
        arrive = _parse_iso(s.get("arrive_buzim"))
        if arrive is None:
            arrive = _parse_iso(s.get("depart")) or datetime.max.replace(tzinfo=TZ)
        return (
            arrive,
            s.get("border_wait_min") if s.get("border_wait_min") is not None else 999,
            s.get("border_cars") if s.get("border_cars") is not None else 99,
        )

    top_future = [s for s in (payload.get("top") or []) if is_upcoming(s)]
    timeline_src = payload.get("timeline") or []
    timeline_future = [dict(t) for t in timeline_src if is_upcoming(t)]

    candidates = list(top_future)
    if not candidates:
        old_best = payload.get("best") or {}
        for t in timeline_future:
            candidates.append(
                {
                    **old_best,
                    "depart": t.get("depart"),
                    "depart_short": t.get("depart_short"),
                    "depart_day": t.get("depart_day"),
                    "depart_label": f"{t.get('depart_day', '')} {t.get('depart_short', '')}".strip(),
                    "arrive_border_short": t.get("arrive_border_short"),
                    "arrive_buzim_short": t.get("arrive_buzim_short"),
                    "border_wait_min": t.get("border_wait_min"),
                    "border_cars": t.get("border_cars"),
                    "total_label": t.get("total_label"),
                    "route_id": "primary",
                    "badge": "nächste beste",
                }
            )

    if not candidates:
        out = dict(payload)
        out["timeline"] = []
        out["top"] = []
        out["best"] = None
        out["best_low_border"] = None
        out["advanced"] = True
        return out

    candidates.sort(key=sort_key)
    new_best = dict(candidates[0])
    old_depart = (payload.get("best") or {}).get("depart")
    rolled = new_best.get("depart") != old_depart
    if rolled:
        new_best["badge"] = "nächste beste"
    else:
        new_best.setdefault(
            "badge", (payload.get("best") or {}).get("badge") or "früheste Ankunft"
        )

    low = [
        s
        for s in candidates
        if s.get("border_cars") is not None and float(s["border_cars"]) <= 3.5
    ]
    best_low = None
    if low:
        low_pick = sorted(low, key=sort_key)[0]
        if low_pick.get("depart") != new_best.get("depart"):
            best_low = dict(low_pick)
            best_low["badge"] = "freie Grenze"

    best_depart = new_best.get("depart")
    timeline = []
    for t in timeline_future:
        row = dict(t)
        row["is_best"] = row.get("depart") == best_depart
        timeline.append(row)

    out = dict(payload)
    out["best"] = new_best
    out["best_low_border"] = best_low
    out["top"] = top_future if top_future else candidates[:10]
    out["timeline"] = timeline
    out["advanced"] = rolled
    if rolled:
        hint = (out.get("hint") or "").strip()
        note = "Verpasste Abfahrt → automatisch nächste beste."
        if note not in hint:
            out["hint"] = f"{hint} {note}".strip() if hint else note
    return out


def optimize(
    *,
    notify: bool = False,
    console_only: bool = False,
    out: str | Path | None = None,
) -> str:
    payload = compute_perfect_payload()
    best = payload["best"]
    best_low = payload.get("best_low_border")

    table = Table(title="Top Abfahrten Bužim → Waiblingen (komplett)")
    table.add_column("Los")
    table.add_column("An Grenze")
    table.add_column("Grenze")
    table.add_column("An Waiblingen")
    table.add_column("Route")
    table.add_column("Total")
    for s in payload["top"]:
        table.add_row(
            s["depart_label"],
            s["arrive_border_label"],
            f"~{s['border_wait_min']}m/{s['border_cars']}A",
            s["arrive_buzim_label"],
            s["route_id"],
            s["total_label"],
        )
    console.print(table)

    lines = [
        f"Stand: {payload['generated_label']} Europe/Berlin",
        "Ziel: schnell ankommen + wenig Stau + kurze Grenze Maljevac (BiH→HR)",
        f"Routing: {payload['provider']}",
        "",
        "🏆 EMPFEHLUNG (früheste Ankunft Waiblingen):",
        f"Los: {best['depart_label']}",
        f"Route: {best['route_title']}",
        f"{best['route_summary']}",
        f"An Grenze ca.: {best['arrive_border_label']}",
        f"Grenzwartezeit ca.: {best['border_wait_min']} min ({best['notes']})",
        f"An Waiblingen ca.: {best['arrive_buzim_label']}",
        f"Gesamt: {best['total_label']} | ~{best['distance_km']} km",
        f"Maps: {best['maps_url']}",
    ]
    if best_low:
        lines += [
            "",
            "🌙 Beste Variante mit freier Grenze (Forecast ≤ ~3.5 Autos):",
            f"Los: {best_low['depart_label']}",
            f"Route: {best_low['route_title']}",
            f"An Grenze ca.: {best_low['arrive_border_label']}",
            f"Grenze ca.: {best_low['border_wait_min']} min / {best_low['border_cars']} Autos",
            f"An Waiblingen ca.: {best_low['arrive_buzim_label']}",
            f"Maps: {best_low['maps_url']}",
        ]
    lines += ["", f"Hinweis: {payload['hint']}"]
    text = "\n".join(lines)
    console.print(Panel(text, title="Perfekte Abfahrt (Rückfahrt)", border_style="green"))

    if out:
        write_perfect_json(out, payload)
        console.print(f"[green]Perfect JSON:[/green] {out}")

    if notify:
        msg = Alert(
            source="PerfectDepart",
            severity="critical",
            title=(
                f"🚗 Perfekte Rückfahrt: {best['depart_short']} → "
                f"Waiblingen ~{best['arrive_buzim_short']}"
            ),
            detail=text,
            location="Bužim → Waiblingen",
            url=best["maps_url"],
            event_id=f"perfect:{best['depart']}:{best['route_id']}",
        )
        build_notifiers(console_only=console_only).send(msg)
    return text
