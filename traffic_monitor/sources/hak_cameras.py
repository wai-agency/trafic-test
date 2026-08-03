"""HAK (Croatian Auto Club) border cameras with optional OpenAI vision analysis.

The live camera JPEGs live under ``https://m.hak.hr/cam.asp?id=<id>``. They are
always surfaced in the dashboard (no key needed). When an ``OPENAI_API_KEY`` is
configured, each analysed camera image is sent to OpenAI vision
(default ``gpt-4o``) to estimate queue length / wait time and enrich routing.
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
DEFAULT_MODEL = "gpt-4o"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

_SEV_RANK = {"clear": 0, "info": 0, "warning": 1, "critical": 2}

# Bump when vision prompt / post-processing changes so CI cache is not reused.
_PROMPT_VERSION = 11

_PROMPT = (
    "Du bist ein Verkehrsanalyst fuer eine Live-Grenzkamera (Kroatien/Bosnien, oft Maljevac). "
    "Analysiere NUR die angegebene Fahrtrichtung.\n\n"
    "ZAEHL-METHODE (wichtig):\n"
    "• Gehe die Kolonne Auto fuer Auto von vorne nach hinten durch — nicht schaetzen aus dem Bauch.\n"
    "• Bei dichter Kolonne bis zu den Kabinen sind oft 15–30 PKW sichtbar; unterschätze nicht.\n"
    "• Zuerst vehicles_visible (klar erkennbare Autos), dann vehicles (Schaetzung nur wenn Ende "
    "abgeschnitten ist).\n\n"
    "HARTE REGEL — nur Einfahrt-/Wartespur der genannten Richtung:\n"
    "• NIEMALS: Gegenrichtung, Parkplaetze, abgestellte Autos, Schatten, Gebaeude.\n"
    "• LKW: Feld trucks, nicht in vehicles/vehicles_visible.\n\n"
    "Kolonnenende:\n"
    "• queue_end_visible=true wenn letztes Auto klar ODER Spur frei/nahezu frei (0–2 Autos).\n"
    "• queue_end_visible=false nur bei dichter Kolonne, die am Bildrand weitergeht.\n"
    "• Bei dichter Kolonne mit unsicherem Ende: leichte Aufschlagschaetzung (+20–40%%), "
    "nicht wild verdoppeln und nicht unter vehicles_visible gehen.\n\n"
    "Antworte AUSSCHLIESSLICH mit kompaktem JSON:\n"
    "{"
    "\"vehicles_visible\": <int klar sichtbare Autos in der genannten Spur>, "
    "\"vehicles\": <int Schaetzung Warteschlange nur dieser Spur>, "
    "\"trucks\": <int LKW>, "
    "\"wait_min\": <int Minuten>, "
    "\"queue_end_visible\": <true|false>, "
    "\"parked_ignored\": <int>, "
    "\"severity\": \"clear\"|\"warning\"|\"critical\", "
    "\"weather\": \"sunny|cloudy|rain|fog|night|unknown\", "
    "\"road\": \"frei|flüssig|stockend|dicht|gesperrt|unbekannt\", "
    "\"summary\": \"<kurz deutsch>\" "
    "}.\n"
    "Severity: unter 5 Autos = clear; 5-20 = warning; ueber 20 = critical; "
    "Wartezeit ~2–3 min pro Auto in der aktiven Spur."
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
                    client,
                    image,
                    api_key,
                    model,
                    name,
                    cam.get("direction", ""),
                    count_hint=str(cam.get("count_hint") or ""),
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
            vehicles_visible = verdict.get("vehicles_visible")
            detail_bits = [verdict.get("summary") or "Kamera-Auswertung"]
            if vehicles_visible is not None and vehicles is not None and vehicles_visible != vehicles:
                detail_bits.append(f"sichtbar ~{vehicles_visible}")
            if vehicles is not None:
                detail_bits.append(f"Fahrzeuge ~{vehicles}")
            if verdict.get("trucks") is not None:
                detail_bits.append(f"LKW ~{verdict['trucks']}")
            if wait is not None:
                detail_bits.append(f"Wartezeit ~{wait} min")
            if verdict.get("parked_ignored"):
                detail_bits.append(f"Parkplatz ignoriert ~{verdict['parked_ignored']}")
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
                        "vehicles_visible": vehicles_visible,
                        "trucks": verdict.get("trucks"),
                        "weather": verdict.get("weather"),
                        "road": verdict.get("road"),
                        "queue_end_visible": verdict.get("queue_end_visible"),
                        "parked_ignored": verdict.get("parked_ignored"),
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
    count_hint: str = "",
) -> dict | None:
    b64 = base64.b64encode(image).decode("ascii")
    prompt = _PROMPT + f"\nKamera: {name}. Fahrtrichtung: {direction or 'unbekannt'}."
    hint = (count_hint or "").strip()
    if hint:
        prompt += f"\n\nKAMERA-SPEZIFISCH:\n{hint}"
    body: dict = {
        "model": model,
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
    # GPT-5 family rejects non-default temperature; older models accept 0.
    if not model.startswith("gpt-5"):
        body["temperature"] = 0
    else:
        # Keep nano cheap/fast for classification-style queue counting
        body["reasoning_effort"] = "minimal"
        # Reasoning tokens count toward this limit — keep headroom for JSON
        body["max_completion_tokens"] = 2000

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = client.post(OPENAI_CHAT_URL, headers=headers, json=body)
    if resp.status_code == 429:
        time.sleep(8)
        resp = client.post(OPENAI_CHAT_URL, headers=headers, json=body)
    if resp.status_code >= 400:
        # Retry once without optional GPT-5-only knobs if the API rejects them
        if model.startswith("gpt-5") and resp.status_code == 400:
            body.pop("reasoning_effort", None)
            body.pop("max_completion_tokens", None)
            resp = client.post(OPENAI_CHAT_URL, headers=headers, json=body)
    if resp.status_code >= 400:
        raise ValueError(f"OpenAI {resp.status_code}: {resp.text[:240]}")
    return parse_vision_response(resp.json())


def parse_vision_response(data: dict) -> dict | None:
    """Extract and normalise the verdict from an OpenAI chat.completions response."""
    try:
        message = data["choices"][0]["message"]
        text = message.get("content")
        # Some reasoning models may return content as a list of parts
        if isinstance(text, list):
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in text
            )
    except (KeyError, IndexError, TypeError):
        return None
    if not text:
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
    for key in ("vehicles", "vehicles_visible", "wait_min", "trucks", "parked_ignored"):
        value = verdict.get(key)
        try:
            out[key] = int(value) if value is not None else None
        except (TypeError, ValueError):
            out[key] = None

    # Prefer explicit visible count; fall back to vehicles for older responses
    visible = out.get("vehicles_visible")
    if visible is None:
        visible = out.get("vehicles")
    if visible is not None:
        out["vehicles_visible"] = visible
    if out.get("vehicles") is None and visible is not None:
        out["vehicles"] = visible
    # Never report fewer cars than clearly visible
    if visible is not None and out.get("vehicles") is not None and out["vehicles"] < visible:
        out["vehicles"] = visible

    # Default false: missing/ambiguous end → worst case (do not assume "frei")
    raw_end = verdict.get("queue_end_visible", False)
    if isinstance(raw_end, str):
        end_visible = raw_end.strip().lower() in ("true", "1", "yes", "ja", "sichtbar")
    elif raw_end is None:
        end_visible = False
    else:
        end_visible = bool(raw_end)

    cars = out.get("vehicles_visible")
    if cars is None:
        cars = out.get("vehicles") or 0
    road = (out.get("road") or "").lower()

    # Light traffic with a claimed cut-off end is usually a false "worst case"
    # (model saw booths / opposite lane). Treat short queues as end-visible.
    if not end_visible and cars <= 4 and road not in ("dicht", "stockend"):
        end_visible = True

    # Dense bumper-to-bumper queues often look "finished" at distant booths
    # while more cars are still in line — treat as end not reliably visible.
    if end_visible and cars >= 7 and road in ("stockend", "dicht"):
        end_visible = False
        summary = out.get("summary") or ""
        note = "Dichte Kolonne — Ende unsicher, Worst Case."
        if "worst" not in summary.lower() and "unsicher" not in summary.lower():
            out["summary"] = f"{summary} {note}".strip() if summary else note

    out["queue_end_visible"] = end_visible

    # Calibrated uplift when end is cut off — never invent huge queues from few cars.
    if not end_visible and cars > 0:
        out["severity"] = "critical"
        out["road"] = "dicht"
        if cars <= 4:
            # Short/ambiguous: keep close to visible count
            estimated = cars + 2
            wait_floor = max(cars * 3, 12)
        elif cars <= 10:
            estimated = max(int(round(cars * 1.4)), cars + 3)
            wait_floor = max(30, estimated * 2)
        else:
            # Dense long queue: modest buffer, not +15 / 2× fantasy
            estimated = max(int(round(cars * 1.5)), cars + 5)
            wait_floor = max(45, estimated * 2)
        base = out.get("vehicles")
        if base is None or base < estimated:
            out["vehicles"] = estimated
        elif base > estimated * 2 and visible is not None:
            # Cap wild model estimates far above visible count
            out["vehicles"] = max(estimated, int(round(visible * 1.6)))
        if out.get("wait_min") is None or out["wait_min"] < wait_floor:
            out["wait_min"] = wait_floor
        summary = out.get("summary") or ""
        note = "Ende nicht sichtbar — leichte Aufschlagschätzung."
        if "aufschlag" not in summary.lower() and "ende nicht" not in summary.lower():
            out["summary"] = f"{summary} {note}".strip() if summary else note

    # Severity for short visible queues when end is visible
    if end_visible and cars is not None:
        if cars < 5 and out["severity"] == "critical" and road not in ("dicht", "gesperrt"):
            out["severity"] = "clear" if cars <= 2 else "warning"

    return out


def _extract_json(text: str) -> object:
    match = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
