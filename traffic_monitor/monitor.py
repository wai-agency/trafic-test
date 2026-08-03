from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from traffic_monitor.config import load_config
from traffic_monitor.models import Alert
from traffic_monitor.notifiers import AlertState, Notifier, build_notifiers
from traffic_monitor.sources import fetch_all

console = Console()


def run_once(
    config_path: str | None = None,
    *,
    min_severity: str = "warning",
    notify: bool = True,
    console_only: bool = False,
    state_path: str | Path = ".traffic_alert_state.json",
) -> list[Alert]:
    config = load_config(config_path)
    alerts = fetch_all(config)
    rank = {"info": 0, "warning": 1, "critical": 2}
    min_rank = rank.get(min_severity, 1)
    actionable = [a for a in alerts if rank.get(a.severity, 0) >= min_rank]

    _print_table(alerts)

    if not notify:
        return alerts

    notifier: Notifier = build_notifiers(console_only=console_only)
    state = AlertState(Path(state_path), cooldown_sec=int(config.get("cooldown_sec") or 1800))
    now_ts = time.time()
    sent = 0
    for alert in actionable:
        if not state.should_send(alert, now_ts):
            continue
        notifier.send(alert)
        state.mark_sent(alert, now_ts)
        sent += 1
    console.print(f"[bold]Alerts gesendet:[/bold] {sent} / actionable {len(actionable)} / total {len(alerts)}")
    return alerts


def watch(
    config_path: str | None = None,
    *,
    interval: int | None = None,
    min_severity: str = "warning",
    console_only: bool = False,
    state_path: str | Path = ".traffic_alert_state.json",
) -> None:
    config = load_config(config_path)
    poll = interval or int(config.get("poll_interval_sec") or 300)
    console.print(f"Watch gestartet — Intervall {poll}s — {datetime.now(timezone.utc).isoformat()}")
    while True:
        try:
            run_once(
                config_path,
                min_severity=min_severity,
                notify=True,
                console_only=console_only,
                state_path=state_path,
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Monitor-Fehler: {exc}[/red]")
        time.sleep(poll)


def _print_table(alerts: list[Alert]) -> None:
    table = Table(title="Aktuelle Meldungen Stuttgart→Bužim Korridor")
    table.add_column("Sev")
    table.add_column("Quelle")
    table.add_column("Ort")
    table.add_column("Titel")
    table.add_column("Delay")
    if not alerts:
        table.add_row("-", "-", "-", "Keine Meldungen / Quellen leer", "-")
    for alert in sorted(alerts, key=lambda a: {"critical": 0, "warning": 1, "info": 2}.get(a.severity, 3)):
        table.add_row(
            alert.severity,
            alert.source,
            alert.location or "-",
            alert.title[:80],
            f"{alert.delay_min}m" if alert.delay_min is not None else "-",
        )
    console.print(table)
