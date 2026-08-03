from __future__ import annotations

import httpx

from traffic_monitor.models import Alert

BASE = "https://verkehr.autobahn.de/o/autobahn"


def fetch_autobahn(config: dict) -> list[Alert]:
    roads = config.get("autobahn_roads") or ["A8"]
    bbox = config.get("autobahn_bbox") or {}
    threshold = int(config.get("autobahn_delay_threshold_sec") or 600)
    alerts: list[Alert] = []

    with httpx.Client(timeout=20.0, headers={"User-Agent": "stuttgart-buzim-traffic/1.0"}) as client:
        for road in roads:
            for endpoint, kind in (("warning", "Stau/Warnung"), ("closure", "Sperrung")):
                url = f"{BASE}/{road}/services/{endpoint}"
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    continue
                payload = resp.json()
                items = payload.get(endpoint) or payload.get(f"{endpoint}s") or []
                if not isinstance(items, list):
                    continue
                for item in items:
                    alert = _item_to_alert(item, road, kind, bbox, threshold)
                    if alert:
                        alerts.append(alert)
    return alerts


def _in_bbox(lat: float, lon: float, bbox: dict) -> bool:
    return (
        float(bbox.get("min_lat", -90)) <= lat <= float(bbox.get("max_lat", 90))
        and float(bbox.get("min_lon", -180)) <= lon <= float(bbox.get("max_lon", 180))
    )


def _item_to_alert(
    item: dict,
    road: str,
    kind: str,
    bbox: dict,
    threshold: int,
) -> Alert | None:
    coord = item.get("coordinate") or {}
    lat = coord.get("lat")
    lon = coord.get("long") or coord.get("lon")
    if lat is None or lon is None:
        return None
    if bbox and not _in_bbox(float(lat), float(lon), bbox):
        return None

    delay_raw = item.get("delayTimeValue")
    delay_sec = int(float(delay_raw)) if delay_raw not in (None, "") else 0
    is_blocked = str(item.get("isBlocked", "")).lower() == "true"
    abnormal = (item.get("abnormalTrafficType") or "").upper()
    display = (item.get("display_type") or "").upper()

    # Skip ordinary long-term roadworks without queue/block
    if display == "ROADWORKS" and not is_blocked and delay_sec < threshold:
        return None

    relevant = (
        is_blocked
        or delay_sec >= threshold
        or abnormal in {"STATIONARY_TRAFFIC", "QUEUING_TRAFFIC"}
    )
    if not relevant:
        return None

    desc = item.get("description") or []
    if isinstance(desc, list):
        detail = " | ".join(str(x) for x in desc if str(x).strip())[:500]
    else:
        detail = str(desc)[:500]

    severity = "critical" if is_blocked or delay_sec >= threshold * 2 else "warning"
    delay_min = round(delay_sec / 60) if delay_sec else None
    title = item.get("title") or f"{road} {kind}"
    location = item.get("subtitle") or road

    return Alert(
        source="autobahn.de",
        severity=severity,
        title=title,
        detail=detail,
        location=location,
        url=f"{BASE}/{road}/services/warning",
        event_id=str(item.get("identifier") or ""),
        delay_min=delay_min,
        extras={"road": road, "abnormalTrafficType": abnormal, "isBlocked": is_blocked},
    )
