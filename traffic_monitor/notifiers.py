from __future__ import annotations

import json
from pathlib import Path

import httpx
from rich.console import Console

from traffic_monitor.config import env
from traffic_monitor.models import Alert

console = Console()


class Notifier:
    def send(self, alert: Alert) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def send(self, alert: Alert) -> None:
        style = {
            "critical": "bold red",
            "warning": "yellow",
            "info": "cyan",
        }.get(alert.severity, "white")
        console.print(alert.to_message(), style=style)
        console.print("-" * 40)


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    def send(self, alert: Alert) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        # Preview an for Maps-Links, sonst aus
        preview = "google.com/maps" in (alert.url or "") or "google.com/maps" in alert.detail
        payload = {
            "chat_id": self.chat_id,
            "text": alert.to_message(),
            "disable_web_page_preview": not preview,
        }
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()


class MultiNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, alert: Alert) -> None:
        for notifier in self.notifiers:
            try:
                notifier.send(alert)
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]Notifier-Fehler ({type(notifier).__name__}): {exc}[/red]")


class AlertState:
    """Deduplicate alerts with cooldown."""

    def __init__(self, path: Path, cooldown_sec: int = 1800) -> None:
        self.path = path
        self.cooldown_sec = cooldown_sec
        self._data: dict[str, float] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {}

    def should_send(self, alert: Alert, now_ts: float) -> bool:
        if alert.severity == "info" and alert.title == "Quelle nicht erreichbar":
            # Don't spam unreachable sources; only first time / after cooldown
            pass
        key = alert.fingerprint()
        last = self._data.get(key)
        if last is not None and now_ts - last < self.cooldown_sec:
            return False
        return True

    def mark_sent(self, alert: Alert, now_ts: float) -> None:
        self._data[alert.fingerprint()] = now_ts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")


def build_notifiers(console_only: bool = False) -> Notifier:
    notifiers: list[Notifier] = [ConsoleNotifier()]
    if console_only:
        return MultiNotifier(notifiers)

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if token and chat_id:
        notifiers.append(TelegramNotifier(token, chat_id))
    return MultiNotifier(notifiers)
