from __future__ import annotations

from traffic_monitor.models import Alert
from traffic_monitor.sources.asfinag import fetch_asfinag
from traffic_monitor.sources.autobahn import fetch_autobahn
from traffic_monitor.sources.gpmaljevac import fetch_gpmaljevac
from traffic_monitor.sources.hak_cameras import fetch_hak_cameras
from traffic_monitor.sources.nakordoni import fetch_nakordoni
from traffic_monitor.sources.promet import fetch_promet


def fetch_all(config: dict) -> list[Alert]:
    alerts: list[Alert] = []
    fetchers = (
        fetch_autobahn,
        fetch_asfinag,
        fetch_promet,
        fetch_gpmaljevac,
        fetch_nakordoni,
        fetch_hak_cameras,
    )
    for fetch in fetchers:
        try:
            alerts.extend(fetch(config))
        except Exception as exc:  # noqa: BLE001 - keep monitor running
            # Soft signal only; never alert-spam unreachable optional sources
            alerts.append(
                Alert(
                    source=fetch.__name__.removeprefix("fetch_"),
                    severity="info",
                    title="Quelle nicht erreichbar",
                    detail=str(exc)[:400],
                    event_id=f"source-down:{fetch.__name__}",
                )
            )
    return alerts
