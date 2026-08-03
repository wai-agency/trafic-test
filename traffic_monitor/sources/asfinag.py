from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from traffic_monitor.models import Alert

FEED = "https://publiccontent.asfinag.at/rss/feed/de/trafficmessages"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def fetch_asfinag(config: dict) -> list[Alert]:
    keywords = [k.lower() for k in (config.get("keywords") or {}).get("critical", [])]
    congestion = [k.lower() for k in (config.get("keywords") or {}).get("congestion", [])]

    with httpx.Client(timeout=20.0, headers={"User-Agent": "stuttgart-buzim-traffic/1.0"}) as client:
        resp = client.get(FEED)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)

    alerts: list[Alert] = []
    for entry in root.findall("a:entry", ATOM_NS):
        title = (entry.findtext("a:title", default="", namespaces=ATOM_NS) or "").strip()
        summary = (entry.findtext("a:summary", default="", namespaces=ATOM_NS) or "").strip()
        updated = (entry.findtext("a:updated", default="", namespaces=ATOM_NS) or "").strip()
        entry_id = (entry.findtext("a:id", default="", namespaces=ATOM_NS) or "").strip()
        text = f"{title} {summary}".lower()

        if keywords and not any(k in text for k in keywords):
            # Still keep hard closures with congestion words on A-roads near corridor
            if not any(k in text for k in congestion):
                continue
            if not any(token in text for token in ("a10", "a11", "a2", "a9", "süd", "tauern", "karawan")):
                continue

        severity = "critical" if any(k in text for k in ("totalsperre", "vollsperre", "gesperrt")) else "warning"
        if any(k in text for k in ("karawan", "tauern", "a11", "a10")):
            severity = "critical" if severity == "warning" and any(c in text for c in congestion) else severity

        alerts.append(
            Alert(
                source="ASFINAG",
                severity=severity if any(c in text for c in congestion) or severity == "critical" else "info",
                title=title or "ASFINAG Meldung",
                detail=(summary or updated)[:500],
                location=_guess_location(text),
                url=FEED,
                event_id=entry_id or f"asfinag:{title}:{updated}",
            )
        )
    return alerts


def _guess_location(text: str) -> str:
    for label, keys in (
        ("Karawanken", ("karawan",)),
        ("Tauern/A10", ("tauern", "a10", "katschberg")),
        ("A11", ("a11",)),
        ("Villach", ("villach",)),
        ("Salzburg/Walserberg", ("walserberg", "salzburg")),
    ):
        if any(k in text for k in keys):
            return label
    return "Österreich Autobahn"
