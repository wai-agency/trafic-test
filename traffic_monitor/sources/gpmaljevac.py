from __future__ import annotations

import re

import httpx

from traffic_monitor.models import Alert

URL = "https://gpmaljevac.com/"

# Must match corridor relevance for Stuttgart→Bužim
CORRIDOR = (
    "karavan",
    "maljevac",
    "izači",
    "izaci",
    "sloven",
    "ljubljana",
    "zagreb",
    "tauern",
    "katschberg",
    "villach",
    "salzburg",
    "walserberg",
    "bužim",
    "buzim",
    "velika kladu",
    "unsko",
)
STRONG_TRAFFIC = (
    "gužv",
    "guzv",
    "kolon",
    "zastoj",
    "blokad",
    "kašnjen",
    "kasnjen",
    "stau",
    "stockend",
    "čep",
    "zatvor",
    "čekanj",
    "cekanj",
)
NOISE = (
    "droge",
    "oružje",
    "oruzje",
    "nakit",
    "autizm",
    "komentar",
    "optužio",
    "optuzio",
    "oduzeo",
    "km/h",
    "pošalji vijest",
    "posalji vijest",
    "hladno oruž",
    "policajac pustio",
    "ujelo",
    "preminuo",
    "motociklist",
    "sveti rok",  # Dalmatien, nicht Bužim-Route
    "gabela",
)


def fetch_gpmaljevac(config: dict) -> list[Alert]:
    """Extract traffic-related headlines from GPMaljevac homepage."""
    with httpx.Client(
        timeout=25.0,
        headers={"User-Agent": "stuttgart-buzim-traffic/1.0"},
        follow_redirects=True,
    ) as client:
        resp = client.get(URL)
        resp.raise_for_status()
        html = resp.text

    titles = _extract_titles(html)
    alerts: list[Alert] = []
    seen: set[str] = set()

    for title in titles:
        lower = title.lower()
        if any(n in lower for n in NOISE):
            continue
        if not any(c in lower for c in CORRIDOR):
            continue
        if not any(h in lower for h in STRONG_TRAFFIC) and not any(
            b in lower for b in ("maljevac", "izači", "izaci", "karavan")
        ):
            continue
        key = lower[:100]
        if key in seen:
            continue
        seen.add(key)

        severity = "critical" if any(
            x in lower for x in ("gužv", "guzv", "kolon", "zastoj", "blokad", "zatvor", "stau", "kašnjen")
        ) else "warning"

        alerts.append(
            Alert(
                source="GPMaljevac",
                severity=severity,
                title=title[:160],
                detail=title[:500],
                location=_location(lower),
                url=URL,
                event_id=f"gpm:{key}",
            )
        )
    return alerts[:20]


def _extract_titles(html: str) -> list[str]:
    # Prefer WordPress-like title anchors; fall back to class=title blocks
    patterns = (
        r'class="[^"]*entry-title[^"]*"[^>]*>\s*<a[^>]*>(.*?)</a>',
        r'class="[^"]*title[^"]*"[^>]*>\s*<a[^>]*>(.*?)</a>',
        r'class="[^"]*title[^"]*"[^>]*>(.*?)</(?:div|span|h\d|p)>',
    )
    found: list[str] = []
    for pat in patterns:
        for raw in re.findall(pat, html, flags=re.I | re.S):
            text = _clean(raw)
            if _looks_like_headline(text):
                found.append(text)

    # Deduplicate preserving order
    out: list[str] = []
    seen: set[str] = set()
    for t in found:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _clean(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&ndash;", "–", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _looks_like_headline(text: str) -> bool:
    if len(text) < 28 or len(text) > 180:
        return False
    if text.lower() in {"vijest", "vijesti", "najčitanije", "kamere"}:
        return False
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}\s*\|\s*\d{2}:\d{2}", text):
        return False
    if "<" in text or "content=" in text or "meta " in text.lower():
        return False
    return True


def _location(text: str) -> str:
    if "maljevac" in text:
        return "Grenze Maljevac"
    if "izači" in text or "izaci" in text:
        return "Grenze Izačić"
    if "karavan" in text:
        return "Karawanken"
    if "sloven" in text:
        return "Slowenien"
    return "Balkan-Korridor"
