"""Facebook page «Granični prijelaz Maljevac» (GPMaljevac) as live source.

Facebook blocks anonymous scraping. When ``FACEBOOK_PAGE_ACCESS_TOKEN`` (or
``FACEBOOK_ACCESS_TOKEN``) is set, we pull recent page posts via Graph API.
Without a token this source returns [] — the WordPress RSS in
``gpmaljevac.py`` still covers the same community regularly.
"""

from __future__ import annotations

import httpx

from traffic_monitor.config import env
from traffic_monitor.models import Alert
from traffic_monitor.sources.gpmaljevac import (
    FACEBOOK_URL,
    _is_relevant,
    _location,
    _severity,
)

PAGE_ID = "100064800966799"  # facebook.com/GPMaljevac
PAGE_USERNAME = "GPMaljevac"
GRAPH = "https://graph.facebook.com/v19.0"


def fetch_facebook_gpmaljevac(config: dict) -> list[Alert]:
    token = env("FACEBOOK_PAGE_ACCESS_TOKEN") or env("FACEBOOK_ACCESS_TOKEN")
    if not token:
        return []

    page = str((config.get("facebook_gpmaljevac") or {}).get("page_id") or PAGE_ID)
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        resp = client.get(
            f"{GRAPH}/{page}/posts",
            params={
                "access_token": token,
                "fields": "id,message,created_time,permalink_url",
                "limit": 15,
            },
        )
        if resp.status_code >= 400:
            # Try username alias
            resp = client.get(
                f"{GRAPH}/{PAGE_USERNAME}/posts",
                params={
                    "access_token": token,
                    "fields": "id,message,created_time,permalink_url",
                    "limit": 15,
                },
            )
        resp.raise_for_status()
        data = resp.json()

    alerts: list[Alert] = []
    seen: set[str] = set()
    for row in data.get("data") or []:
        message = (row.get("message") or "").strip()
        if not message:
            continue
        blob = message.lower()
        if not _is_relevant(blob):
            continue
        key = (row.get("id") or message[:80]).lower()
        if key in seen:
            continue
        seen.add(key)
        title = message.split("\n", 1)[0][:160]
        url = row.get("permalink_url") or FACEBOOK_URL
        alerts.append(
            Alert(
                source="Facebook GPMaljevac",
                severity=_severity(blob),
                title=title,
                detail=message[:500],
                location=_location(blob),
                url=url,
                event_id=f"fb-gpm:{key}",
                extras={
                    "facebook": FACEBOOK_URL,
                    "created_time": row.get("created_time") or "",
                },
            )
        )
    return alerts[:20]
