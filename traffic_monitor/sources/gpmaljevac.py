from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import unescape
from email.utils import parsedate_to_datetime

import httpx

from traffic_monitor.models import Alert

PORTAL_URL = "https://gpmaljevac.com/"
FEED_URL = "https://gpmaljevac.com/feed/"
FACEBOOK_URL = "https://www.facebook.com/GPMaljevac"

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
    "granič",
    "granic",
    "granica",
    "prijelaz",
    "prekoid",
    "bih",
    "hrvatsk",
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
    "haos",
    "promet",
    "čeka",
    "ceka",
    "warte",
    "queue",
    "kilometr",
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
    "romobil",
    "plaž",
    "plaz",
    "onečišćen",
    "oneciscen",
    "lidl",
    "konkurs",
    "ubistvo",
    "ubijen",
    "dozvole",
    "bolovanje",
)


def fetch_gpmaljevac(config: dict) -> list[Alert]:
    """Pull recent GPMaljevac posts (same community as the Facebook page).

    Primary: WordPress RSS (updated throughout the day — mirrors what the
    Facebook page / community publishes about borders & corridor traffic).
    Fallback: homepage headline scrape.
    """
    alerts = _from_rss()
    if not alerts:
        alerts = _from_homepage()
    return alerts[:25]


def _from_rss() -> list[Alert]:
    with httpx.Client(
        timeout=25.0,
        headers={"User-Agent": "stuttgart-buzim-traffic/1.0"},
        follow_redirects=True,
    ) as client:
        resp = client.get(FEED_URL)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)

    alerts: list[Alert] = []
    seen: set[str] = set()
    for item in root.findall("./channel/item"):
        title = _clean(item.findtext("title") or "")
        link = (item.findtext("link") or PORTAL_URL).strip()
        desc = _clean(item.findtext("description") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
        blob = f"{title} {desc}".lower()
        if not _is_relevant(blob):
            continue
        key = title.lower()[:100] or link
        if key in seen:
            continue
        seen.add(key)
        alerts.append(
            Alert(
                source="GPMaljevac",
                severity=_severity(blob),
                title=title[:160] or "GPMaljevac Meldung",
                detail=(desc or title)[:500],
                location=_location(blob),
                url=link or FACEBOOK_URL,
                event_id=f"gpm-rss:{key}",
                extras={
                    "facebook": FACEBOOK_URL,
                    "pub_date": item.findtext("pubDate") or "",
                },
            )
        )
    return alerts


def _from_homepage() -> list[Alert]:
    with httpx.Client(
        timeout=25.0,
        headers={"User-Agent": "stuttgart-buzim-traffic/1.0"},
        follow_redirects=True,
    ) as client:
        resp = client.get(PORTAL_URL)
        resp.raise_for_status()
        html = resp.text

    alerts: list[Alert] = []
    seen: set[str] = set()
    for title in _extract_titles(html):
        lower = title.lower()
        if not _is_relevant(lower):
            continue
        key = lower[:100]
        if key in seen:
            continue
        seen.add(key)
        alerts.append(
            Alert(
                source="GPMaljevac",
                severity=_severity(lower),
                title=title[:160],
                detail=title[:500],
                location=_location(lower),
                url=PORTAL_URL,
                event_id=f"gpm:{key}",
                extras={"facebook": FACEBOOK_URL},
            )
        )
    return alerts


def _severity(blob: str) -> str:
    if any(
        x in blob
        for x in ("gužv", "guzv", "kolon", "zastoj", "blokad", "zatvor", "stau", "kašnjen", "haos", "kilometr")
    ):
        return "critical"
    return "warning"


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
    text = unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&ndash;", "–", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    # WordPress RSS footer pollutes every item with "gpmaljevac / granični prijelaz"
    text = re.sub(
        r"(The post|appeared first on).*?(gpmaljevac|granični prijelaz).*",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _has_token(blob: str, token: str) -> bool:
    """Substring match for longer tokens; word-ish boundary for short ones."""
    if len(token) >= 4:
        return token in blob
    return re.search(rf"(?<![a-zčćžšđ]){re.escape(token)}(?![a-zčćžšđ])", blob) is not None


def _is_relevant(blob: str) -> bool:
    blob = blob.lower()
    hard_noise = (
        "ubistvo",
        "ubijen",
        "lidl",
        "plaž",
        "plaz",
        "romobil",
        "dozvole",
        "bolovanje",
        "onečišćen",
        "konkurs",
        "utopio",
        "utopila",
    )
    if any(n in blob for n in hard_noise):
        return False
    if any(n in blob for n in NOISE) and not any(
        _has_token(blob, b) for b in ("maljevac", "izači", "izaci", "karavan")
    ):
        return False

    corridor_hit = any(_has_token(blob, c) for c in CORRIDOR)
    traffic_hit = any(_has_token(blob, h) for h in STRONG_TRAFFIC)
    border_hit = any(
        _has_token(blob, b)
        for b in ("maljevac", "izači", "izaci", "karavan", "izačić")
    )
    # Generic "granični prijelaz" alone is not enough (portal boilerplate)
    if border_hit and traffic_hit:
        return True
    if corridor_hit and traffic_hit and (
        border_hit
        or any(_has_token(blob, x) for x in ("sloven", "karavan", "zagreb", "tauern", "bih"))
    ):
        return True
    return False


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


def parse_pub_date(value: str):
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
