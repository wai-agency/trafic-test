from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

console = Console()
TZ = ZoneInfo("Europe/Berlin")

# OSRM uses lon,lat — return trip Bužim → Waiblingen
BUZIM = (16.0324, 45.0613)
MALJEVAC = (15.7875, 45.2005)
ZAGREB = (15.9819, 45.8150)
LJUBLJANA = (14.5058, 46.0569)
KARAWANKEN = (14.0182, 46.4994)
WAIBLINGEN = (9.3163822, 48.8325659)


def recommend_departure(now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    # Prefer very early morning next travel day
    candidates = []
    for day_offset in (0, 1):
        day = (now + timedelta(days=day_offset)).date()
        for hour, minute, label in (
            (3, 0, "Sehr früh (beste Stau-Chance)"),
            (4, 30, "Früh"),
            (21, 0, "Nachtfahrt (nur wenn du ausgeschlafen bist)"),
        ):
            dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
            if dt <= now:
                continue
            weekday = dt.strftime("%A")
            score = _score(dt)
            candidates.append((score, dt, label, weekday))

    candidates.sort(key=lambda x: (-x[0], x[1]))
    lines = ["## Beste Abfahrt (Bužim → Waiblingen)", ""]
    if not candidates:
        lines.append("Keine zukünftige Slot-Empfehlung berechnet.")
        return "\n".join(lines)

    best = candidates[0]
    lines.append(
        f"**Empfehlung:** {best[1].strftime('%a %d.%m.%Y %H:%M')} Europe/Berlin — {best[2]}"
    )
    lines.append("")
    lines.append("Weitere sinnvolle Slots:")
    for score, dt, label, _weekday in candidates[:5]:
        lines.append(f"- {dt.strftime('%a %d.%m.%Y %H:%M')} — {label} (Score {score}/100)")
    return "\n".join(lines)


def _score(dt: datetime) -> int:
    score = 50
    # Tue-Thu best
    if dt.weekday() in (1, 2, 3):
        score += 25
    elif dt.weekday() == 0:
        score += 10
    elif dt.weekday() == 4:
        score -= 20
    else:
        score -= 30

    hour = dt.hour
    if 3 <= hour <= 5:
        score += 25
    elif hour >= 21 or hour <= 2:
        score += 15
    elif 6 <= hour <= 9:
        score -= 10
    elif 14 <= hour <= 19:
        score -= 25
    return max(0, min(100, score))


def fetch_route_summary() -> str:
    coords = [BUZIM, MALJEVAC, ZAGREB, LJUBLJANA, KARAWANKEN, WAIBLINGEN]
    coord_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{coord_str}?overview=false&steps=false"
    )
    with httpx.Client(timeout=30.0, headers={"User-Agent": "stuttgart-buzim-traffic/1.0"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    route = (data.get("routes") or [None])[0]
    if not route:
        return "Route konnte nicht berechnet werden."

    km = route["distance"] / 1000
    hours = route["duration"] / 3600
    legs = route.get("legs") or []
    labels = [
        "Bužim → Maljevac",
        "Maljevac → Zagreb",
        "Zagreb → Ljubljana",
        "Ljubljana → Karawanken",
        "Karawanken → Waiblingen",
    ]
    lines = [
        "## Beste Route (Hauptkorridor, Rückfahrt)",
        "",
        "**Bužim** → Velika Kladuša → **Maljevac** → Zagreb → SI-A2 Ljubljana → "
        "**A11 Karawanken** → Villach → **A10 Tauern** → Salzburg → A8 → **Waiblingen**",
        "",
        f"OSRM freier Verkehr: **~{km:.0f} km / ~{hours:.1f} h** (ohne Pausen, Stau, Grenze).",
        "Realistisch Sommer: **12–15 h**, mit Stau/Grenze auch mehr.",
        "",
        "### Etappen",
    ]
    for i, leg in enumerate(legs):
        label = labels[i] if i < len(labels) else f"Leg {i}"
        lines.append(
            f"- {label}: {leg['distance']/1000:.0f} km / {leg['duration']/3600:.1f} h"
        )
    lines += [
        "",
        "### Alternative Grenze",
        "Wenn Maljevac (BiH→HR) steht: Ausreise über **Izačić** (bei Bihać), dann zurück auf den Korridor.",
        "",
        "### Was du beachten solltest",
        "- **Österreich:** Digitale Vignette (ASFINAG) + **Sondermaut A10 Tauern** + **Karawanken A11**",
        "- **Slowenien:** E-Vignette vorab (`evinjeta.dars.si`)",
        "- **Kroatien:** Streckenmaut (Ticket/ENC)",
        "- **BiH:** Reisepass/Personalausweis je nach Staatsangehörigkeit; grüne Karte/Versicherung prüfen",
        "- Apps live: ASFINAG, DarsPromet+, HAK, GPMaljevac, Google Maps/Waze",
        "- Tank vor teuren Autobahnstationen, Wasser, Pausen alle 2–3 h",
        "- Meiden: Freitag 14–20, Samstag 06–15, Sonntag Abend Rückreisewelle",
    ]
    return "\n".join(lines)


def print_travel_plan() -> None:
    md = "\n\n".join([recommend_departure(), fetch_route_summary()])
    console.print(Panel(Markdown(md), title="Reiseplan Bužim → Waiblingen", border_style="green"))
