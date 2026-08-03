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
    }
    with httpx.Client(timeout=25.0, headers=headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()

    checkpoints = (
        data.get("checkpoints")
        or data.get("data")
        or data.get("items")
        or (data if isinstance(data, list) else [])
    )
    if isinstance(data, dict) and not checkpoints:
        # Some responses nest under results/border
        for key in ("results", "border", "queues"):
            if isinstance(data.get(key), list):
                checkpoints = data[key]
                break

    alerts: list[Alert] = []
    for cp in checkpoints or []:
        if not isinstance(cp, dict):
            continue
        name = str(cp.get("name") or cp.get("title") or cp.get("checkpoint") or "")
        if name_filters and not any(f in name.lower() for f in name_filters):
            continue

        wait = _extract_wait_min(cp)
        cars = cp.get("cars") or cp.get("queue") or cp.get("vehicles")
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
        stale = cp.get("stale")
        if stale:
            detail_bits.append("Daten ggf. veraltet")

        alerts.append(
            Alert(
                source="Nakordoni",
                severity=severity,
                title=f"Grenze: {name or 'HR↔BA'}",
                detail=" | ".join(detail_bits) or str(cp)[:400],
                location=name or "HR-BA Grenze",
                url="https://nakordoni.eu/en/stat/16/17/4",
                event_id=str(cp.get("id") or cp.get("ppid") or name),
                delay_min=wait,
            )
        )
    return alerts


def _extract_wait_min(cp: dict) -> int | None:
    for key in ("wait_min", "waitMinutes", "waiting_time", "eta_min", "queue_time_min", "time"):
        val = cp.get(key)
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        # Heuristic: values > 10 hours are probably seconds
        if num > 600:
            return int(num / 60)
        return int(num)
    return None
