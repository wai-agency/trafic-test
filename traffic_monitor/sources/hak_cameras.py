"""HAK (Croatian Auto Club) border cameras with optional OpenAI vision analysis.

The live camera JPEGs live under ``https://m.hak.hr/cam.asp?id=<id>``. They are
always surfaced in the dashboard (no key needed). When an ``OPENAI_API_KEY`` is
configured, each analysed camera image is sent to a cheap OpenAI vision model
(default ``gpt-4o-mini``) to estimate queue length / wait time and enrich routing.
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
DEFAULT_MODEL = "gpt-4o-mini"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

_SEV_RANK = {"clear": 0, "info": 0, "warning": 1, "critical": 2}

# Bump when vision prompt / post-processing changes so CI cache is not reused.
_PROMPT_VERSION = 4

_PROMPT = (
    "Du bist ein Verkehrsanalyst fuer eine Live-Grenzkamera (Kroatien/Bosnien). "
    "Analysiere NUR die angegebene Fahrtrichtung.\n\n"
    "Was zaehlen (streng):\n"
    "1) Nur Fahrzeuge in der AKTIVEN Warteschlange der genannten Richtung "
    "(Fahrspur Richtung Grenze / Markierung BiH bzw. HR auf dem Bild).\n"
    "2) Zaehle sichtbare Autos in dieser Spur sorgfaeltig — auch kleine/unscharfe "
    "hinten in derselben Spur. Ein Auto = 1.\n"
    "3) NICHT zaehlen: parkende Autos auf Parkplaetzen/am Rand, Gegenrichtung, "
    "LKW nur als trucks (nicht doppelt in vehicles), Schatten, Gebaeude, "
    "Fahrzeuge unter dem Dach der Grenzanlage wenn sie nicht klar in der Schlange sind.\n"
    "4) queue_end_visible=true wenn das Ende der Schlange ODER die Grenzkabine/"
    "Schranke sichtbar ist und die Spur davor nicht ueber den Bildrand hinaus "
    "weiter dicht besetzt wirkt. Bei diesem Bildtyp (Kabinen/Dach im Hintergrund "
    "sichtbar) ist das Ende meist sichtbar → true.\n"
    "5) queue_end_visible=false NUR wenn die Kolonne klar am Bildrand/Huegel "
    "abgeschnitten ist und dahinter noch Schlange vermutet werden muss. Dann "
    "darfst du vehicles leicht erhoehen (sichtbar + vorsichtige Schaetzung), "
    "aber NICHT verdoppeln oder wild aufblasen. Typisch +20–40%, nicht +100%.\n"
    "6) vehicles = realistische Anzahl wartender PKW/Transporter in der aktiven "
    "Schlange (nicht 'alle Autos irgendwo im Bild').\n\n"
    "Antworte AUSSCHLIESSLICH mit kompaktem JSON:\n"
    "{"
    "\"vehicles\": <int wartende PKW/Transporter in der aktiven Schlange>, "
    "\"trucks\": <int LKW in/neben der Schlange>, "
    "\"wait_min\": <int geschaetzte Wartezeit Minuten>, "
    "\"queue_end_visible\": <true|false>, "
    "\"severity\": \"clear\"|\"warning\"|\"critical\", "
    "\"weather\": \"sunny|cloudy|rain|fog|night|unknown\", "
    "\"road\": \"frei|flüssig|stockend|dicht|gesperrt|unbekannt\", "
    "\"summary\": \"<kurze deutsche Lagebeschreibung>\" "
    "}.\n"
    "Richtwerte: unter 5 und Ende sichtbar = clear; "
    "5-20 = warning; ueber 20 oder Ende nicht sichtbar mit langer Schlange = critical. "
    "Wartezeit grob: ~1.5–2.5 min pro Auto in der aktiven Schlange. "
    "Wenn queue_end_visible=false: wait_min etwas hoeher, aber vehicles bleibt nah am sichtbaren Stand."
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
                "role": str(cam.get("role") or ""),
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


_CACHE_TTL_SEC = 12 * 60
# Overridable in tests
_CACHE_PATH: Path | None = None


def _cache_path() -> Path:
    if _CACHE_PATH is not None:
        return _CACHE_PATH
    return Path(env("HAK_CAM_CACHE", ".camera_ai_cache.json") or ".camera_ai_cache.json")


def _load_cache() -> dict:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if time.time() - float(data.get("ts") or 0) > _CACHE_TTL_SEC:
        return {}
    if int(data.get("prompt_v") or 0) != _PROMPT_VERSION:
        return {}
    return data


def _save_cache(alerts: list[Alert], model: str) -> None:
    payload = {
        "ts": time.time(),
        "model": model,
        "prompt_v": _PROMPT_VERSION,
        "alerts": [
            {
                "severity": a.severity,
                "title": a.title,
                "detail": a.detail,
                "location": a.location,
                "url": a.url,
                "event_id": a.event_id,
                "delay_min": a.delay_min,
                "extras": a.extras,
            }
            for a in alerts
        ],
    }
    try:
        _cache_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _alerts_from_cache(data: dict) -> list[Alert]:
    out: list[Alert] = []
    for row in data.get("alerts") or []:
        out.append(
            Alert(
                source="HAK-Cam",
                severity=str(row.get("severity") or "info"),
                title=str(row.get("title") or "Kamera"),
                detail=str(row.get("detail") or ""),
                location=str(row.get("location") or ""),
                url=row.get("url"),
                event_id=str(row.get("event_id") or f"hakcam-cache:{row.get('location')}"),
                delay_min=row.get("delay_min"),
                extras=dict(row.get("extras") or {}),
            )
        )
    return out


def fetch_hak_cameras(config: dict) -> list[Alert]:
    """Analyse configured HAK cameras with OpenAI vision (needs OPENAI_API_KEY).

    Results are cached ~12 min so monitor / perfect / dashboard share one quota.
    Clear/info verdicts are still returned for the dashboard; Telegram filters them.
    """
    hak = config.get("hak_cameras") or {}
    cams = hak.get("cams") or []
    if not cams:
        return []

    api_key = env("OPENAI_API_KEY")
    if not api_key:
        console.print("[yellow]HAK-Cam: OPENAI_API_KEY fehlt — nur Live-Bilder[/yellow]")
        return []

    cached = _load_cache()
    # Only reuse cache when it has real verdicts (empty = previous rate-limit cooldown)
    if cached and cached.get("alerts"):
        n = len(cached["alerts"])
        console.print(f"[dim]HAK-Cam: Cache ({n} verdicts)[/dim]")
        return _alerts_from_cache(cached)

    model = env("OPENAI_VISION_MODEL", hak.get("model") or DEFAULT_MODEL) or DEFAULT_MODEL
    page = str(hak.get("page") or HAK_REFERER)

    alerts: list[Alert] = []
    with httpx.Client(timeout=60.0, headers={"User-Agent": "stuttgart-buzim-traffic/1.0"}) as client:
        for cam in cams:
            # Default: only analyse explicitly marked cams (relevant or analyze=true)
            analyze = cam.get("analyze")
            if analyze is None:
                analyze = bool(cam.get("relevant", False))
            if not analyze:
                continue
            cam_id = cam.get("id")
            if cam_id is None:
                continue
            name = str(cam.get("name") or f"HAK Kamera {cam_id}")
            try:
                image = _download_image(client, cam_id)
                verdict = _analyze_with_openai(
                    client, image, api_key, model, name, cam.get("direction", "")
                )
            except httpx.HTTPError as exc:
                console.print(f"[yellow]HAK-Cam {cam_id}: HTTP {exc}[/yellow]")
                continue
            except ValueError as exc:
                msg = str(exc)
                console.print(f"[yellow]HAK-Cam {cam_id}: {msg}[/yellow]")
                if "429" in msg:
                    _save_cache([], model)
                    console.print("[yellow]HAK-Cam: OpenAI Rate-Limit — später erneut[/yellow]")
                    return alerts
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
            if verdict.get("queue_end_visible") is False:
                detail_bits.append("Kolonnenende nicht sichtbar")
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
                        "role": str(cam.get("role") or ""),
                        "vehicles": vehicles,
                        "trucks": verdict.get("trucks"),
                        "weather": verdict.get("weather"),
                        "road": verdict.get("road"),
                        "queue_end_visible": verdict.get("queue_end_visible"),
                        "relevant": bool(cam.get("relevant", False)),
                    },
                )
            )
    _save_cache(alerts, model)
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


def _analyze_with_openai(
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
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                            # high: distant / tiny cars at the back of the queue
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = client.post(OPENAI_CHAT_URL, headers=headers, json=body)
    if resp.status_code == 429:
        time.sleep(8)
        resp = client.post(OPENAI_CHAT_URL, headers=headers, json=body)
    if resp.status_code >= 400:
        raise ValueError(f"OpenAI {resp.status_code}: {resp.text[:240]}")
    return parse_vision_response(resp.json())


def parse_vision_response(data: dict) -> dict | None:
    """Extract and normalise the verdict from an OpenAI chat.completions response."""
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return normalize_verdict(text)


# Back-compat alias for older tests / imports
def parse_gemini_response(data: dict) -> dict | None:
    """Deprecated alias — accepts either Gemini-shaped or OpenAI-shaped payloads."""
    if isinstance(data, dict) and "choices" in data:
        return parse_vision_response(data)
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    return normalize_verdict(text)


def normalize_verdict(text: str | dict) -> dict | None:
    if isinstance(text, dict):
        verdict = text
    else:
        verdict = _extract_json(str(text))
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

    raw_end = verdict.get("queue_end_visible", True)
    if isinstance(raw_end, str):
        end_visible = raw_end.strip().lower() in ("true", "1", "yes", "ja", "sichtbar")
    else:
        end_visible = bool(raw_end) if raw_end is not None else True
    out["queue_end_visible"] = end_visible

    # Safety floor: if the queue continues beyond the frame, never treat as "frei"
    cars = out.get("vehicles") or 0
    if not end_visible and cars > 0:
        if _SEV_RANK.get(out["severity"], 0) < _SEV_RANK["warning"]:
            out["severity"] = "warning"
        wait = out.get("wait_min")
        # Mild bump only — do not invent a huge queue from a short visible line
        floor = max(15, min(35, int(cars * 2)))
        if wait is None or wait < floor:
            out["wait_min"] = floor
        if out.get("road") in ("frei", "flüssig", "unbekannt"):
            out["road"] = "stockend"
        summary = out.get("summary") or ""
        if "ende" not in summary.lower():
            out["summary"] = (
                (summary + " " if summary else "")
                + "Kolonnenende nicht sichtbar."
            ).strip()
        if cars >= 25 and _SEV_RANK.get(out["severity"], 0) < _SEV_RANK["critical"]:
            out["severity"] = "critical"
            if (out.get("wait_min") or 0) < 40:
                out["wait_min"] = 40

    return out


def _extract_json(text: str) -> object:
    match = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
