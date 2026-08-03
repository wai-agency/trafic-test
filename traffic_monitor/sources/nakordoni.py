from __future__ import annotations

import httpx

from traffic_monitor.config import env
from traffic_monitor.models import Alert


def fetch_nakordoni(config: dict) -> list[Alert]:
    """Optional border-queue API (needs NAKORDONI_API_KEY)."""
    api_key = env("NAKORDONI_API_KEY")
    if not api_key:
        return []

    nk = config.get("nakordoni") or {}
    origin = nk.get("origin", 16)
    destination = nk.get("destination", 17)
    crossing_type = nk.get("crossing_type", 4)
    threshold = int(nk.get("wait_threshold_min") or 45)
    name_filters = [n.lower() for n in nk.get("name_filters") or []]

    url = f"https://nakordoni.eu/api/v2/data/border/{origin}/{destination}/{crossing_type}?lang=de"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "stuttgart-buzim-traffic/1.0",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=25.0, headers=headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()

    checkpoints = _extract_checkpoints(data)
    alerts: list[Alert] = []
    for cp in checkpoints:
        if not isinstance(cp, dict):
            continue
        name = str(cp.get("name") or cp.get("title") or cp.get("checkpoint") or "")
        if name_filters and not any(f in name.lower() for f in name_filters):
            continue

        wait = _extract_wait_min(cp)
        cars = _extract_queue_cars(cp)
        if wait is None and cars is None:
            continue
        if wait is not None and wait < threshold:
            continue
        if wait is None and isinstance(cars, (int, float)) and cars < 30:
            continue

        severity = "critical" if (wait or 0) >= threshold * 1.5 else "warning"
        detail_bits = []
        if cars is not None:
            detail_bits.append(f"Fahrzeuge in Warteschlange: {cars}")
        if wait is not None:
            detail_bits.append(f"Geschätzte Wartezeit: ~{wait} min")
        status = cp.get("wait_status")
        if status:
            detail_bits.append(f"Status: {status}")
        if cp.get("stale"):
            detail_bits.append("Daten ggf. veraltet")
        age = cp.get("age_min")
        if age is not None:
            detail_bits.append(f"Alter: {age} min")

        alerts.append(
            Alert(
                source="Nakordoni",
                severity=severity,
                title=f"Grenze: {name or 'HR↔BA'}",
                detail=" | ".join(detail_bits) or str(cp)[:400],
                location=name or "HR-BA Grenze",
                url=str(cp.get("source_url") or "https://nakordoni.eu/en/stat/16/17/4"),
                event_id=str(cp.get("ppid") or cp.get("id") or name),
                delay_min=wait,
                extras={
                    "cars": cars,
                    "stale": bool(cp.get("stale")),
                    "age_min": age,
                    "wait_status": status,
                    "ppid": cp.get("ppid"),
                },
            )
        )
    return alerts


def snapshot_borders(config: dict) -> list[dict]:
    """Always return current border queues for the dashboard (no alert threshold)."""
    api_key = env("NAKORDONI_API_KEY")
    if not api_key:
        return []

    nk = config.get("nakordoni") or {}
    origin = nk.get("origin", 16)
    destination = nk.get("destination", 17)
    crossing_type = nk.get("crossing_type", 4)
    name_filters = [n.lower() for n in nk.get("name_filters") or []]

    url = f"https://nakordoni.eu/api/v2/data/border/{origin}/{destination}/{crossing_type}?lang=de"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "stuttgart-buzim-traffic/1.0",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=25.0, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        return []

    out: list[dict] = []
    for cp in _extract_checkpoints(data):
        if not isinstance(cp, dict):
            continue
        name = str(cp.get("name") or cp.get("title") or "")
        if name_filters and not any(f in name.lower() for f in name_filters):
            continue
        cars = _extract_queue_cars(cp)
        wait = _extract_wait_min(cp)
        if cars is None and wait is None:
            continue
        out.append(
            {
                "name": name,
                "cars": cars,
                "wait_min": wait,
                "stale": bool(cp.get("stale")),
                "age_min": cp.get("age_min"),
                "wait_status": cp.get("wait_status"),
                "url": str(cp.get("source_url") or "https://nakordoni.eu/en/stat/16/17/4"),
                "ppid": cp.get("ppid"),
            }
        )
    # Maljevac first
    out.sort(key=lambda b: (0 if "maljevac" in b["name"].lower() else 1, b["name"]))
    return out


def _extract_checkpoints(data: object) -> list:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    nested = data.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("checkpoints"), list):
        return nested["checkpoints"]
    if isinstance(nested, list):
        return nested

    for key in ("checkpoints", "items", "results", "border", "queues"):
        val = data.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict) and isinstance(val.get("checkpoints"), list):
            return val["checkpoints"]
    return []


def _extract_wait_min(cp: dict) -> int | None:
    for key in ("wait_min", "waitMinutes", "waiting_time", "eta_min", "queue_time_min"):
        val = cp.get(key)
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        return int(num)
    return None


def _extract_queue_cars(cp: dict) -> int | None:
    for key in ("queue", "cars", "vehicles", "vehicles_count", "car_count"):
        val = cp.get(key)
        if val is None:
            continue
        try:
            return int(float(val))
        except (TypeError, ValueError):
            continue
    return None
