"""HAK (Croatian Auto Club) border cameras with optional Gemini vision analysis.

The live camera JPEGs live under ``https://m.hak.hr/cam.asp?id=<id>``. They are
always surfaced in the dashboard (no key needed). When a ``GEMINI_API_KEY`` is
configured, each analysed camera image is sent to Google Gemini Flash (vision) to
estimate the queue length / wait time and enrich other routing data.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import httpx
from rich.console import Console

from traffic_monitor.config import env
from traffic_monitor.models import Alert

console = Console()

HAK_REFERER = "https://m.hak.hr/"
DEFAULT_MODEL = "gemini-2.0-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_SEV_RANK = {"clear": 0, "info": 0, "warning": 1, "critical": 2}

_PROMPT = (
    "Du bist ein Verkehrsanalyst. Das Bild ist eine Live-Grenzkamera an der "
    "kroatisch-bosnischen Grenze. Schaetze die Lage in der angegebenen "
    "Fahrtrichtung. Antworte AUSSCHLIESSLICH mit kompaktem JSON:\n"
    "{"
    "\"vehicles\": <int wartende Fahrzeuge in der aktiven Schlange>, "
    "\"trucks\": <int LKW sichtbar>, "
    "\"wait_min\": <int geschaetzte Wartezeit Minuten>, "
    "\"severity\": \"clear\"|\"warning\"|\"critical\", "
    "\"weather\": \"sunny|cloudy|rain|fog|night|unknown\", "
    "\"road\": \"frei|flüssig|stockend|dicht|gesperrt|unbekannt\", "
    "\"summary\": \"<kurze deutsche Lagebeschreibung>\" "
    "}. "
    "Richtwerte: unter 5 Fahrzeuge = clear, 5-15 = warning, ueber 15 = critical. "
    "Wenn keine Fahrzeuge erkennbar sind, severity=clear."
)


def camera_image_url(cam_id: object) -> str:
    return f"https://m.hak.hr/cam.asp?id={cam_id}"


def cameras_from_config(config: dict) -> list[dict]:
    """Normalised camera list for the dashboard (independent of the AI key)."""
    hak = config.get("hak_cameras") or {}
    cams = hak.get("cams") or []
    out: list[dict] = []
    for cam in cams:
        cam_id = cam.get("id")
        if cam_id is None:
            continue
        out.append(
            {
                "id": cam_id,
                "name": str(cam.get("name") or f"HAK Kamera {cam_id}"),
                "direction": str(cam.get("direction") or ""),
                "relevant": bool(cam.get("relevant", False)),
                "image_url": camera_image_url(cam_id),
            }
        )
    return out


def snapshot_cameras(config: dict, out_dir: str | Path) -> dict[int | str, str]:
    """Download current JPEGs into ``out_dir/cams/`` for reliable dashboard hosting."""
    root = Path(out_dir)
    cams_dir = root / "cams"
    cams_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    mapping: dict[int | str, str] = {}
    with httpx.Client(timeout=25.0, headers={"User-Agent": "stuttgart-buzim-traffic/1.0"}) as client:
        for cam in cameras_from_config(config):
            cam_id = cam["id"]
            try:
                data = _download_image(client, cam_id)
            except (httpx.HTTPError, ValueError):
                continue
            path = cams_dir / f"{cam_id}.jpg"
            path.write_bytes(data)
            mapping[cam_id] = f"cams/{cam_id}.jpg?t={stamp}"
    return mapping


def fetch_hak_cameras(config: dict) -> list[Alert]:
    """Analyse configured HAK cameras with Gemini vision (needs GEMINI_API_KEY).

    Clear/info verdicts are still returned so the dashboard and other modules can
    use them; Telegram typically filters below ``warning``.
    """
    hak = config.get("hak_cameras") or {}
    cams = hak.get("cams") or []
    if not cams:
        return []

    api_key = env("GEMINI_API_KEY")
    if not api_key:
        return []

    model = env("GEMINI_MODEL", hak.get("model") or DEFAULT_MODEL)
    page = str(hak.get("page") or HAK_REFERER)

    alerts: list[Alert] = []
    with httpx.Client(timeout=45.0, headers={"User-Agent": "stuttgart-buzim-traffic/1.0"}) as client:
        for cam in cams:
            if not cam.get("analyze", True):
                continue
            cam_id = cam.get("id")
            if cam_id is None:
                continue
            name = str(cam.get("name") or f"HAK Kamera {cam_id}")
            try:
                image = _download_image(client, cam_id)
                verdict = _analyze_with_gemini(
                    client, image, api_key, model, name, cam.get("direction", "")
                )
            except httpx.HTTPError as exc:
                console.print(f"[yellow]HAK-Cam {cam_id}: HTTP {exc}[/yellow]")
                continue
            except ValueError as exc:
                console.print(f"[yellow]HAK-Cam {cam_id}: {exc}[/yellow]")
                continue
            if verdict is None:
                console.print(f"[yellow]HAK-Cam {cam_id}: leere/ungültige KI-Antwort[/yellow]")
                continue
            console.print(
                f"[green]HAK-Cam {cam_id}:[/green] {verdict.get('severity')} · "
                f"~{verdict.get('wait_min')} min · {verdict.get('summary', '')[:60]}"
            )

            severity = verdict["severity"]
            if severity == "clear":
                severity = "info"

            wait = verdict.get("wait_min")
            vehicles = verdict.get("vehicles")
            detail_bits = [verdict.get("summary") or "Kamera-Auswertung"]
            if vehicles is not None:
                detail_bits.append(f"Fahrzeuge ~{vehicles}")
            if verdict.get("trucks") is not None:
                detail_bits.append(f"LKW ~{verdict['trucks']}")
            if wait is not None:
                detail_bits.append(f"Wartezeit ~{wait} min")
            if verdict.get("weather"):
                detail_bits.append(f"Wetter: {verdict['weather']}")
            if verdict.get("road"):
                detail_bits.append(f"Lage: {verdict['road']}")
            detail_bits.append(f"KI: {model}")

            alerts.append(
                Alert(
                    source="HAK-Cam",
                    severity=severity,
                    title=f"Kamera: {name}",
                    detail=" | ".join(detail_bits),
                    location=name,
                    url=camera_image_url(cam_id),
                    event_id=f"hakcam:{cam_id}:{severity}:{(wait or 0) // 15}",
                    delay_min=wait,
                    extras={
                        "image_url": camera_image_url(cam_id),
                        "page_url": page,
                        "direction": str(cam.get("direction") or ""),
                        "cam_id": cam_id,
                        "vehicles": vehicles,
                        "trucks": verdict.get("trucks"),
                        "weather": verdict.get("weather"),
                        "road": verdict.get("road"),
                        "relevant": bool(cam.get("relevant", False)),
                    },
                )
            )
    return alerts


def maljevac_cam_wait_min(alerts: list[Alert]) -> int | None:
    """Best live wait estimate from HAK cameras for Maljevac HR→BiH."""
    waits: list[int] = []
    for alert in alerts:
        if alert.source != "HAK-Cam" or alert.delay_min is None:
            continue
        blob = f"{alert.title} {alert.location} {alert.extras.get('direction', '')}".lower()
        if "maljevac" in blob and ("hr" in blob and "bih" in blob or "ausreise" in blob or "hr->" in blob):
            waits.append(int(alert.delay_min))
        elif "maljevac" in blob and alert.extras.get("relevant"):
            waits.append(int(alert.delay_min))
    if not waits:
        # fallback: any Maljevac cam
        for alert in alerts:
            if alert.source != "HAK-Cam" or alert.delay_min is None:
                continue
            if "maljevac" in f"{alert.title} {alert.location}".lower():
                waits.append(int(alert.delay_min))
    return max(waits) if waits else None


def _download_image(client: httpx.Client, cam_id: object) -> bytes:
    resp = client.get(camera_image_url(cam_id), headers={"Referer": HAK_REFERER})
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "image" not in ctype.lower():
        raise ValueError(f"unexpected content-type: {ctype!r}")
    return resp.content


def _analyze_with_gemini(
    client: httpx.Client,
    image: bytes,
    api_key: str,
    model: str,
    name: str,
    direction: str,
) -> dict | None:
    b64 = base64.b64encode(image).decode("ascii")
    prompt = _PROMPT + f"\nKamera: {name}. Fahrtrichtung: {direction or 'unbekannt'}."
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": "image/jpeg", "data": b64}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }
    resp = client.post(GEMINI_URL.format(model=model), params={"key": api_key}, json=body)
    if resp.status_code >= 400:
        raise ValueError(f"Gemini {resp.status_code}: {resp.text[:240]}")
    return parse_gemini_response(resp.json())


def parse_gemini_response(data: dict) -> dict | None:
    """Extract and normalise the verdict from a Gemini generateContent response."""
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    verdict = _extract_json(text)
    if not isinstance(verdict, dict):
        return None

    severity = str(verdict.get("severity", "")).lower()
    if severity not in _SEV_RANK:
        severity = "warning"
    out: dict = {
        "severity": severity,
        "summary": str(verdict.get("summary") or "").strip(),
        "weather": str(verdict.get("weather") or "unknown").lower(),
        "road": str(verdict.get("road") or "unbekannt"),
    }
    for key in ("vehicles", "wait_min", "trucks"):
        value = verdict.get(key)
        try:
            out[key] = int(value) if value is not None else None
        except (TypeError, ValueError):
            out[key] = None
    return out


def _extract_json(text: str) -> object:
    match = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
