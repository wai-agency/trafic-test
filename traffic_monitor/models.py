from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class Alert:
    source: str
    severity: str  # info | warning | critical
    title: str
    detail: str
    location: str = ""
    url: str = ""
    event_id: str = ""
    delay_min: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def fingerprint(self) -> str:
        base = self.event_id or f"{self.source}|{self.title}|{self.location}"
        return base.strip().lower()

    def to_message(self) -> str:
        parts = [
            f"[{self.severity.upper()}] {self.source}: {self.title}",
        ]
        if self.location:
            parts.append(f"Ort: {self.location}")
        if self.delay_min is not None:
            parts.append(f"Verzögerung: ~{self.delay_min} min")
        if self.detail:
            parts.append(self.detail)
        if self.url:
            parts.append(self.url)
        return "\n".join(parts)
