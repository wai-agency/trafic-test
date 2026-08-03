from __future__ import annotations

import httpx

from traffic_monitor.config import env
from traffic_monitor.models import Alert

DEFAULT_URL = "https://www.promet.si/dc/b2b.dogodki.json?language=en_US"


def fetch_promet(config: dict) -> list[Alert]:
    """Slovenia DARS/promet.si events (free JSON when reachable)."""
    url = env("PROMET_JSON_URL", DEFAULT_URL)
    keywords = [k.lower() for k in (config.get("keywords") or {}).get("critical", [])]
    congestion = [k.lower() for k in (config.get("keywords") or {}).get("congestion", [])]

    with httpx.Client(timeout=20.0, headers={"User-Agent": "stuttgart-buzim-traffic/1.0"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()

    items = data if isinstance(data, list) else data.get("events") or data.get("Items") or data.get("data") or []
    if not isinstance(items, list):
        return []

    alerts: list[Alert] = []
    for item in items:
        if not isinstance(item, dict):
            text = str(item)
            title = text[:120]
            detail = text
            event_id = ""
        else:
            title = str(
                item.get("title")
                or item.get("Title")
                or item.get("opis")
                or item.get("Description")
                or item.get("road")
                or "Promet event"
            )
            detail = str(
                item.get("description")
                or item.get("Description")
                or item.get("content")
                or item.get("Content")
                or item.get("tekst")
                or ""
            )
            event_id = str(item.get("id") or item.get("Id") or item.get("guid") or "")
            text = f"{title} {detail}"

        lower = text.lower()
        if keywords and not any(k in lower for k in keywords):
            continue
        severity = "critical" if any(c in lower for c in congestion) else "info"
        if "karav" in lower:
            severity = "critical" if severity != "info" else "warning"

        alerts.append(
            Alert(
                source="promet.si",
                severity=severity,
                title=title[:160],
                detail=detail[:500],
                location="Slowenien" if "karav" not in lower else "Karavanke (SI)",
                url=url,
                event_id=event_id or f"promet:{title[:80]}",
            )
        )
    return alerts
