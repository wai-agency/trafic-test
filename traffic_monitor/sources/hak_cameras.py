"""HAK (Croatian Auto Club) border cameras with optional OpenAI vision analysis.

Still-frame JPEGs come from the desktop HAK videowall
(``https://www.hak.hr/info/kamere/<id>.jpg``, typically 1280×720). The mobile
page ``https://m.hak.hr/kamera.asp?...`` only serves 640×360 via ``cam.asp``.
Dashboard always embeds snapshots (no key needed). With ``OPENAI_API_KEY``,
images are sent to OpenAI vision (default ``gpt-5.6-terra``) for queue length.
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

# Desktop videowall stills are sharper than m.hak.hr/cam.asp (640×360).
HAK_IMAGE_BASE = "https://www.hak.hr/info/kamere"
HAK_REFERER = "https://www.hak.hr/info/kamere/"
HAK_MOBILE_PAGE = "https://m.hak.hr/kamera.asp?g=2&k=177"
DEFAULT_MODEL = "gpt-5.6-terra"
# Fallbacks if Terra refuses / returns empty (keep a classic vision model in the chain)
FALLBACK_MODELS = ("gpt-4o-mini", "gpt-4o")
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

_SEV_RANK = {"clear": 0, "info": 0, "warning": 1, "critical": 2}

# Bump when vision prompt / post-processing / model cadence / image source changes.
_PROMPT_VERSION = 18

# Reuse OpenAI vision results ~20 min so scheduled runs do not re-upload JPEGs every cycle.
_CACHE_TTL_SEC = 20 * 60

_PROMPT = (
    "Du hilfst Autofahrern bei der Reiseplanung (Rueckfahrt Bužim → Waiblingen). "
    "Vor dir ist ein oeffentliches Live-Bild einer Strassenkamera an einem Grenzubergang. "
    "Schätze die Warteschlange NUR in der genannten Fahrtrichtung.\n\n"
    "Zaehlmethode:\n"
    "• Gehe die Kolonne Auto fuer Auto von vorne nach hinten durch.\n"
    "• Bei dichter Kolonne bis zu den Kabinen oft 15–30 PKW — nicht unterschaetzen.\n"
    "• vehicles_visible = klar erkennbare PKW in der genannten Spur; "
    "vehicles = Schaetzung der Warteschlange (nicht unter vehicles_visible).\n"
    "• Nicht mitzaehlen: Gegenrichtung, Parkplaetze, abgestellte Autos.\n"
    "• LKW nur im Feld trucks.\n\n"
    "queue_end_visible=true wenn das letzte Auto klar ist oder die Spur frei wirkt. "
    "false nur bei dichter Kolonne, die am Bildrand weitergeht "
    "(dann leichte Aufschlagschaetzung +20–40%%).\n\n"
    "Antworte NUR als JSON:\n"
    "{"
    "\"vehicles_visible\": <int>, "
    "\"vehicles\": <int>, "
    "\"trucks\": <int>, "
    "\"wait_min\": <int>, "
    "\"queue_end_visible\": <true|false>, "
    "\"parked_ignored\": <int>, "
    "\"severity\": \"clear\"|\"warning\"|\"critical\", "
    "\"weather\": \"sunny|cloudy|rain|fog|night|unknown\", "
    "\"road\": \"frei|flüssig|stockend|dicht|gesperrt|unbekannt\", "
    "\"summary\": \"<kurz deutsch>\" "
    "}.\n"
    "Severity: queue_end_visible=false → critical; "
    "Ende sichtbar und <=6 Autos → clear/warning; >20 Autos → critical. "
    "Wartezeit ~2–3 min/Auto."
)


def camera_image_url(cam_id: object) -> str:
    """HD still from www.hak.hr videowall (usually 1280×720)."""
    return f"{HAK_IMAGE_BASE}/{cam_id}.jpg"


def camera_mobile_page_url(cam_id: object | None = None) -> str:
    """Mobile HAK camera group page (Maljevac / Velika Kladuša)."""
    return HAK_MOBILE_PAGE


def cameras_from_config(config: dict) -> list[dict]:
    """Normalised camera list for the dashboard (independent of the AI key)."""
    hak = config.get("hak_cameras") or {}
    cams = hak.get("cams") or []
    image_base = str(hak.get("image_base") or HAK_IMAGE_BASE).rstrip("/")
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
                "image_url": f"{image_base}/{cam_id}.jpg",
                "page_url": str(hak.get("page") or HAK_MOBILE_PAGE),
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
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BuzimLine/1.0; +https://github.com/wai-agency/trafic-test)",
        "Referer": HAK_REFERER,
        "Accept": "image/jpeg,image/*;q=0.8,*/*;q=0.5",
    }
    with httpx.Client(timeout=25.0, headers=headers, follow_redirects=True) as client:
        for cam in cameras_from_config(config):
            cam_id = cam["id"]
            try:
                data = _download_image(client, cam_id, image_url=cam.get("image_url"))
            except (httpx.HTTPError, ValueError):
                continue
            path = cams_dir / f"{cam_id}.jpg"
            path.write_bytes(data)
            mapping[cam_id] = f"cams/{cam_id}.jpg?t={stamp}"
    return mapping


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
    page = str(hak.get("page") or HAK_MOBILE_PAGE)
    image_base = str(hak.get("image_base") or HAK_IMAGE_BASE).rstrip("/")

    to_analyze = []
    for cam in cams:
        analyze = cam.get("analyze")
        if analyze is None:
            analyze = bool(cam.get("relevant", False))
        if analyze and cam.get("id") is not None:
            to_analyze.append(cam)

    alerts: list[Alert] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BuzimLine/1.0; +https://github.com/wai-agency/trafic-test)",
        "Referer": HAK_REFERER,
        "Accept": "image/jpeg,image/*;q=0.8,*/*;q=0.5",
    }
    with httpx.Client(timeout=60.0, headers=headers, follow_redirects=True) as client:
        for cam in to_analyze:
            cam_id = cam.get("id")
            name = str(cam.get("name") or f"HAK Kamera {cam_id}")
            direction = cam.get("direction", "")
            hint = str(cam.get("count_hint") or "")
            try:
                image = _download_image(
                    client, cam_id, image_url=f"{image_base}/{cam_id}.jpg"
                )
                verdict, used_model = _analyze_with_fallback(
                    client, image, api_key, model, name, direction, count_hint=hint
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
            detail_bits.append(f"KI: {used_model}")

            alerts.append(
                Alert(
                    source="HAK-Cam",
                    severity=severity,
                    title=f"Kamera: {name}",
                    detail=" | ".join(detail_bits),
                    location=name,
                    url=page,
                    event_id=f"hakcam:{cam_id}:{severity}:{(wait or 0) // 15}",
                    delay_min=wait,
                    extras={
                        "image_url": f"{image_base}/{cam_id}.jpg",
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
    # Only cache complete sets — partial cache hides a failed direction for ~12 min
    if len(alerts) >= len(to_analyze):
        _save_cache(alerts, model)
    elif alerts:
        console.print(
            f"[yellow]HAK-Cam: unvollständig ({len(alerts)}/{len(to_analyze)}) — kein Cache[/yellow]"
        )
    return alerts


def maljevac_cam_wait_min(alerts: list[Alert]) -> int | None:
    """Best live wait estimate from HAK cameras for Maljevac BiH→HR (return)."""
    waits: list[int] = []
    for alert in alerts:
        if alert.source != "HAK-Cam" or alert.delay_min is None:
            continue
        role = str(alert.extras.get("role") or "").lower()
        blob = f"{alert.title} {alert.location} {alert.extras.get('direction', '')}".lower()
        # Prefer return direction BiH → HR
        if role == "to_hr" or (
            "maljevac" in blob
            and ("bih->hr" in blob or "bih → hr" in blob or "einreise" in blob)
        ):
            waits.append(int(alert.delay_min))
        elif "maljevac" in blob and alert.extras.get("relevant") and role != "to_bih":
            waits.append(int(alert.delay_min))
    if not waits:
        for alert in alerts:
            if alert.source != "HAK-Cam" or alert.delay_min is None:
                continue
            if "maljevac" in f"{alert.title} {alert.location}".lower():
                waits.append(int(alert.delay_min))
    return max(waits) if waits else None


def _download_image(
    client: httpx.Client,
    cam_id: object,
    *,
    image_url: str | None = None,
) -> bytes:
    url = image_url or camera_image_url(cam_id)
    # Cache-bust so we do not reuse a CDN-stale frame for vision / snapshots.
    sep = "&" if "?" in url else "?"
    resp = client.get(f"{url}{sep}t={int(time.time())}", headers={"Referer": HAK_REFERER})
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "image" not in ctype.lower():
        raise ValueError(f"unexpected content-type: {ctype!r}")
    data = resp.content
    # Prefer HD (≥720p). Mobile cam.asp is 640×360 — reject tiny frames if possible.
    if len(data) < 20_000:
        raise ValueError(f"image too small ({len(data)} bytes) from {url}")
    return data


def _analyze_with_fallback(
    client: httpx.Client,
    image: bytes,
    api_key: str,
    primary_model: str,
    name: str,
    direction: str,
    count_hint: str = "",
) -> tuple[dict | None, str]:
    """Try primary model, then fallbacks, when the model refuses or returns empty JSON."""
    models: list[str] = [primary_model]
    for m in FALLBACK_MODELS:
        if m not in models:
            models.append(m)
    last: dict | None = None
    used = primary_model
    for idx, model in enumerate(models):
        used = model
        last = _analyze_with_openai(
            client, image, api_key, model, name, direction, count_hint=count_hint
        )
        if last is not None:
            if idx > 0:
                console.print(f"[dim]HAK-Cam: Fallback-Modell {model}[/dim]")
            return last, model
        if idx + 1 < len(models):
            console.print(f"[dim]HAK-Cam: {model} leer/refusal — versuche {models[idx + 1]}…[/dim]")
            time.sleep(1)
    return last, used


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
    # GPT-5 / 5.6 family: no custom temperature; use low/none reasoning for counting.
    if not model.startswith("gpt-5"):
        body["temperature"] = 0
    else:
        # 5.6 supports none|low|…; older gpt-5-nano used "minimal"
        if model.startswith("gpt-5.6") or model.startswith("gpt-5.5") or model.startswith("gpt-5.4"):
            body["reasoning_effort"] = "low"
        else:
            body["reasoning_effort"] = "minimal"
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
    data = resp.json()
    verdict = parse_vision_response(data)
    if verdict is None:
        # Surface refusals / empty content for logs
        try:
            msg = data["choices"][0]["message"]
            refusal = msg.get("refusal")
            content = msg.get("content")
            finish = data["choices"][0].get("finish_reason")
            console.print(
                f"[dim]HAK-Cam parse miss finish={finish!r} refusal={str(refusal)[:80]!r} "
                f"content={str(content)[:120]!r}[/dim]"
            )
        except (KeyError, IndexError, TypeError):
            console.print(f"[dim]HAK-Cam parse miss raw={str(data)[:160]!r}[/dim]")
    return verdict


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
        if not text and message.get("refusal"):
            return None
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

    # ~6 Autos mit sichtbarem Ende: noch ok, nicht als Stau markieren.
    if end_visible and cars is not None:
        if cars <= 6 and out["severity"] == "critical" and road not in ("dicht", "gesperrt"):
            out["severity"] = "clear" if cars <= 3 else "warning"
        elif cars <= 3 and out.get("severity") == "warning" and road not in ("dicht", "stockend", "gesperrt"):
            out["severity"] = "clear"

    return out


def _extract_json(text: str) -> object:
    match = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
