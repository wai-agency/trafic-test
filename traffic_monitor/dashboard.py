from __future__ import annotations

import html
import json
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rich.console import Console

from traffic_monitor.config import load_config
from traffic_monitor.models import Alert
from traffic_monitor.perfect_depart import load_perfect_json
from traffic_monitor.recommend import TZ, _score, recommend_departure
from traffic_monitor.sources import fetch_all
from traffic_monitor.sources.hak_cameras import cameras_from_config, snapshot_cameras
from traffic_monitor.sources.nakordoni import snapshot_borders

console = Console()

ROUTE_STOPS = [
    ("Waiblingen", "Start"),
    ("Salzburg", "AT"),
    ("Tauern", "A10"),
    ("Karawanken", "A11"),
    ("Zagreb", "HR"),
    ("Maljevac", "Grenze"),
    ("Bužim", "Ziel"),
]


def build_dashboard_payload(config_path: str | None = None, *, perfect: dict | None = None) -> dict:
    config = load_config(config_path)
    return _payload_from_alerts(config, fetch_all(config), perfect=perfect)


def _payload_from_alerts(
    config: dict,
    alerts: list[Alert],
    *,
    perfect: dict | None = None,
) -> dict:
    now = datetime.now(TZ)
    actionable = [a for a in alerts if a.severity in {"warning", "critical"} and a.title != "Quelle nicht erreichbar"]
    critical_n = sum(1 for a in actionable if a.severity == "critical")
    warning_n = sum(1 for a in actionable if a.severity == "warning")

    if critical_n:
        status = "critical"
        status_label = "Stau / Störung"
        status_hint = "Aktive kritische Meldungen auf dem Korridor — früh starten oder umplanen."
    elif warning_n:
        status = "warning"
        status_label = "Erhöhtes Risiko"
        status_hint = "Hinweise vorhanden. Vor Abfahrt nochmal prüfen."
    else:
        status = "clear"
        status_label = "Korridor ruhig"
        status_hint = "Keine kritischen Treffer in den Live-Quellen."

    best = _best_slot(now)
    route = config.get("route") or {}

    cam_verdicts = {a.location: a for a in alerts if a.source == "HAK-Cam"}
    cameras = []
    for cam in cameras_from_config(config):
        verdict = cam_verdicts.get(cam["name"])
        extras = verdict.extras if verdict else {}
        cameras.append(
            {
                **cam,
                "severity": verdict.severity if verdict else None,
                "verdict": verdict.detail if verdict else None,
                "wait_min": verdict.delay_min if verdict else None,
                "vehicles": extras.get("vehicles"),
                "trucks": extras.get("trucks"),
                "weather": extras.get("weather"),
                "road": extras.get("road"),
                "queue_end_visible": extras.get("queue_end_visible"),
                "live_url": cam["image_url"],
            }
        )

    borders = snapshot_borders(config)  # secondary reference only

    def _cam_side(role: str, direction_bits: tuple[str, ...]) -> dict | None:
        for c in cameras:
            role_ok = (c.get("role") or "") == role
            blob = f"{c.get('name','')} {c.get('direction','')}".lower()
            dir_ok = any(b in blob for b in direction_bits)
            if (role_ok or dir_ok) and "maljevac" in blob:
                if c.get("vehicles") is None and c.get("wait_min") is None:
                    continue
                return {
                    "name": c.get("name"),
                    "direction": c.get("direction"),
                    "cars": c.get("vehicles"),
                    "wait_min": c.get("wait_min"),
                    "trucks": c.get("trucks"),
                    "severity": c.get("severity"),
                    "note": (c.get("verdict") or "")[:140],
                    "cam_id": c.get("id"),
                    "queue_end_visible": c.get("queue_end_visible"),
                }
        return None

    to_bih = _cam_side("to_bih", ("hr->bih", "ausreise", "hr → bih", "hr→bih"))
    to_hr = _cam_side("to_hr", ("bih->hr", "einreise", "bih → hr", "bih→hr"))
    maljevac_now = None
    if to_bih or to_hr:
        # Headline uses outbound to BiH (your direction), fallback inbound
        primary = to_bih or to_hr
        maljevac_now = {
            "name": "Maljevac",
            "source": "HAK-Cam · gpt-4o-mini",
            "cars": primary.get("cars"),
            "wait_min": primary.get("wait_min"),
            "trucks": primary.get("trucks"),
            "stale": False,
            "note": "Live gezählt von HAK-Kameras (OpenAI Vision)",
            "to_bih": to_bih,
            "to_hr": to_hr,
        }

    return {
        "generated_at": now.isoformat(),
        "generated_label": now.strftime("%d.%m.%Y %H:%M"),
        "tz": "Europe/Berlin",
        "brand": "BuzimLine",
        "from": route.get("from", "Waiblingen"),
        "to": route.get("to", "Bužim"),
        "via": route.get("via", ""),
        "approx_km": route.get("approx_km", 960),
        "status": status,
        "status_label": status_label,
        "status_hint": status_hint,
        "counts": {"critical": critical_n, "warning": warning_n, "total": len(alerts)},
        "best_departure": best,
        "perfect": perfect,
        "stops": ROUTE_STOPS,
        "maljevac_now": maljevac_now,
        "borders": borders,
        "cameras": cameras,
        "alerts": [
            {
                "severity": a.severity,
                "source": a.source,
                "title": a.title,
                "detail": a.detail,
                "location": a.location,
                "url": a.url,
                "delay_min": a.delay_min,
            }
            for a in sorted(
                [a for a in alerts if a.title != "Quelle nicht erreichbar"],
                key=lambda x: {"critical": 0, "warning": 1, "info": 2}.get(x.severity, 3),
            )
        ],
        "sources_down": [
            {"source": a.source, "detail": a.detail}
            for a in alerts
            if a.title == "Quelle nicht erreichbar"
        ],
        "checklist": [
            "AT-Vignette + Tauern- & Karawanken-Maut",
            "SI E-Vignette (evinjeta.dars.si)",
            "HR-Maut / Karte bereithalten",
            "Dokumente & Versicherung BiH",
            "Wasser, Pausen alle 2–3 h",
        ],
        "links": [
            {"label": "ASFINAG", "url": "https://www.asfinag.at/"},
            {"label": "promet.si", "url": "https://www.promet.si/"},
            {"label": "HAK", "url": "https://www.hak.hr/info/stanje-na-cestama"},
            {"label": "GPMaljevac", "url": "https://gpmaljevac.com/"},
            {"label": "Nakordoni Grenze", "url": "https://nakordoni.eu/en/stat/16/17/4"},
        ],
        "recommend_md": recommend_departure(now),
    }


def _best_slot(now: datetime) -> dict:
    from datetime import timedelta

    candidates = []
    for day_offset in (0, 1, 2):
        day = (now + timedelta(days=day_offset)).date()
        for hour, minute, label in (
            (3, 0, "Sehr früh"),
            (4, 30, "Früh"),
            (21, 0, "Nachtfahrt"),
        ):
            dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
            if dt <= now:
                continue
            candidates.append((_score(dt), dt, label))
    candidates.sort(key=lambda x: (-x[0], x[1]))
    if not candidates:
        return {"label": "—", "when": "—", "score": 0}
    score, dt, label = candidates[0]
    return {
        "label": label,
        "when": dt.strftime("%a %d.%m. %H:%M"),
        "iso": dt.isoformat(),
        "score": score,
    }


def _perfect_section(perfect: dict | None) -> str:
    if not perfect or not perfect.get("best"):
        return """
    <section class="perfect perfect-empty" aria-labelledby="perfect-title">
      <h2 id="perfect-title">Perfekte Abfahrt</h2>
      <p class="empty">Noch keine Live-Optimierung. Wird mit dem nächsten Monitor-Lauf berechnet.</p>
    </section>
    """

    best = perfect["best"]
    alt = perfect.get("best_low_border")
    timeline = perfect.get("timeline") or []
    top = perfect.get("top") or []

    timeline_html = "".join(
        f"""
        <li class="tl-item load-{html.escape(t.get('load', 'ok'))}{' best' if t.get('is_best') else ''}{' night' if t.get('night') else ''}">
          <span class="tl-depart">{html.escape(t.get('depart_day', ''))} {html.escape(t['depart_short'])}</span>
          <span class="tl-bar" aria-hidden="true"></span>
          <span class="tl-meta">Grenze ~{t['border_wait_min']}m · Bužim {html.escape(t['arrive_buzim_short'])}</span>
        </li>
        """
        for t in timeline[:28]
    )

    top_html = "".join(
        f"""
        <li class="slot-row">
          <div class="slot-when">
            <strong>{html.escape(s['depart_short'])}</strong>
            <span>{html.escape(s['depart_day'])}</span>
          </div>
          <div class="slot-path">
            <span>Grenze {html.escape(s['arrive_border_short'])} · ~{s['border_wait_min']} min</span>
            <span>{html.escape(s['route_id'])} · {html.escape(s['total_label'])}</span>
          </div>
          <div class="slot-arrive">
            <strong>{html.escape(s['arrive_buzim_short'])}</strong>
            <span>Bužim</span>
          </div>
        </li>
        """
        for s in top[:6]
    )

    alt_html = ""
    if alt:
        alt_html = f"""
        <div class="perfect-alt">
          <div class="alt-kicker">Freiere Grenze</div>
          <div class="alt-grid">
            <div><span>Los</span><strong>{html.escape(alt['depart_label'])}</strong></div>
            <div><span>Grenze</span><strong>~{alt['border_wait_min']} min</strong></div>
            <div><span>Bužim</span><strong>{html.escape(alt['arrive_buzim_short'])}</strong></div>
          </div>
          <a class="maps-btn ghost" href="{html.escape(alt['maps_url'])}" target="_blank" rel="noopener">Maps öffnen</a>
        </div>
        """

    stops = best.get("stops") or []
    journey = " → ".join(html.escape(x.split(",")[0]) for x in stops) if stops else html.escape(
        best.get("route_summary") or ""
    )

    return f"""
    <section class="perfect" aria-labelledby="perfect-title">
      <div class="perfect-head">
        <h2 id="perfect-title">Perfekte Abfahrt</h2>
        <span class="perfect-stamp">Live · {html.escape(perfect.get('generated_label', ''))}</span>
      </div>

      <div class="perfect-hero" id="perfect-hero" data-perfect-root>
        <p class="perfect-kicker">Waiblingen → Bužim · komplette Route</p>
        <p class="perfect-time" id="perfect-time">{html.escape(best['depart_short'])}</p>
        <p class="perfect-day" id="perfect-day">{html.escape(best['depart_day'])} · {html.escape(best.get('badge') or 'Empfehlung')}</p>

        <ol class="journey" aria-label="Reisezeitlinie">
          <li>
            <span class="j-label">Los</span>
            <strong id="perfect-los">{html.escape(best['depart_short'])}</strong>
            <span class="j-sub">Waiblingen</span>
          </li>
          <li class="j-line" aria-hidden="true"></li>
          <li>
            <span class="j-label">Grenze</span>
            <strong id="perfect-border">{html.escape(best['arrive_border_short'])}</strong>
            <span class="j-sub" id="perfect-border-meta">~{best['border_wait_min']} min · {best['border_cars']} Autos</span>
          </li>
          <li class="j-line" aria-hidden="true"></li>
          <li>
            <span class="j-label">Ziel</span>
            <strong id="perfect-arrive">{html.escape(best['arrive_buzim_short'])}</strong>
            <span class="j-sub">Bužim</span>
          </li>
        </ol>

        <div class="perfect-stats">
          <div><span>Gesamt</span><strong id="perfect-total">{html.escape(best['total_label'])}</strong></div>
          <div><span>Distanz</span><strong id="perfect-dist">~{best['distance_km']} km</strong></div>
          <div><span>Route</span><strong id="perfect-route-id">{html.escape(best['route_id'])}</strong></div>
        </div>

        <p class="perfect-route">{journey}</p>
        <a class="maps-btn" id="perfect-maps" href="{html.escape(best['maps_url'])}" target="_blank" rel="noopener">In Google Maps öffnen</a>
        <p class="perfect-note" id="perfect-note">{html.escape(perfect.get('hint', ''))}</p>
      </div>

      {alt_html}

      <div class="perfect-timeline-wrap">
        <h3>Abfahrtsfenster (Hauptroute)</h3>
        <p class="empty" style="margin:0 0 10px">
          Inkl. Nacht 00–05 · Update alle ~{html.escape(str(perfect.get('refresh_minutes') or 30))} Min
          · {len(timeline)} Slots
        </p>
        <ol class="timeline">{timeline_html or '<li class="empty">Keine Slots</li>'}</ol>
      </div>

      <div class="perfect-slots-wrap">
        <h3>Top Alternativen</h3>
        <ol class="slot-list">{top_html}</ol>
      </div>
    </section>
    """


def render_html(payload: dict) -> str:
    alerts_html = "".join(_alert_row(a) for a in payload["alerts"]) or (
        '<p class="empty">Keine relevanten Meldungen gerade.</p>'
    )
    downs_html = "".join(
        f'<li><strong>{html.escape(d["source"])}</strong> — {html.escape(d["detail"][:120])}</li>'
        for d in payload["sources_down"]
    )
    stops_html = "".join(
        f'<li><span class="stop-name">{html.escape(n)}</span><span class="stop-tag">{html.escape(t)}</span></li>'
        for n, t in payload["stops"]
    )
    checks_html = "".join(f"<li>{html.escape(c)}</li>" for c in payload["checklist"])
    cameras_html = "".join(_camera_card(c) for c in payload.get("cameras", []))
    links_html = "".join(
        f'<a href="{html.escape(l["url"])}" target="_blank" rel="noopener">{html.escape(l["label"])}</a>'
        for l in payload["links"]
    )
    perfect_html = _perfect_section(payload.get("perfect"))
    border_html = _border_section(payload.get("maljevac_now"), payload.get("borders") or [])
    # Strip perfect blob from embedded JSON? Keep it — useful for clients.
    payload_json = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)

    hero_depart = (
        payload["perfect"]["best"]["depart_label"]
        if payload.get("perfect") and payload["perfect"].get("best")
        else payload["best_departure"]["when"]
    )
    hero_arrive = (
        f"Bužim ~{payload['perfect']['best']['arrive_buzim_short']}"
        if payload.get("perfect") and payload["perfect"].get("best")
        else "siehe Empfehlung"
    )
    mj = payload.get("maljevac_now") or {}

    def _hero_side(side: dict | None) -> str:
        if not side or side.get("cars") is None:
            return "—"
        cars = side["cars"]
        wait = side.get("wait_min")
        if wait is None:
            return f"{cars} Autos"
        return f"{cars} Autos · ~{wait} min"

    hero_bih = _hero_side(mj.get("to_bih"))
    hero_hr = _hero_side(mj.get("to_hr"))
    # Fallback if only legacy single-side fields are present
    if hero_bih == "—" and hero_hr == "—" and mj.get("cars") is not None:
        wait = mj.get("wait_min")
        hero_bih = (
            f"{mj['cars']} Autos · ~{wait} min" if wait is not None else f"{mj['cars']} Autos"
        )

    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#1f3d36" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <title>{html.escape(payload["brand"])} · {html.escape(payload["from"])} → {html.escape(payload["to"])}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=Manrope:wght@400;600;700&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --ink: #14231f;
      --muted: #4d635c;
      --paper: #f3f0e7;
      --panel: rgba(255,252,245,0.82);
      --line: #d7c7a1;
      --accent: #c47a12;
      --accent-2: #1f6b57;
      --critical: #b42318;
      --warning: #b54708;
      --clear: #0f6b4c;
      --shadow: 0 18px 50px rgba(20, 35, 31, 0.12);
      --radius: 22px;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      font-family: "Manrope", system-ui, sans-serif;
      color: var(--ink);
      min-height: 100vh;
      background:
        radial-gradient(1200px 600px at 10% -10%, #d9ebe3 0%, transparent 55%),
        radial-gradient(900px 500px at 100% 0%, #f0dfb8 0%, transparent 50%),
        linear-gradient(180deg, #eef3ef 0%, var(--paper) 45%, #e7efe9 100%);
      background-attachment: fixed;
    }}
    .wrap {{
      width: min(720px, 100%);
      margin: 0 auto;
      padding: max(16px, env(safe-area-inset-top)) 16px calc(28px + env(safe-area-inset-bottom));
    }}
    .brand-row {{
      display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
      margin-bottom: 10px;
    }}
    .brand {{
      font-family: "Fraunces", Georgia, serif;
      font-weight: 700;
      font-size: clamp(2rem, 8vw, 2.8rem);
      letter-spacing: -0.03em;
      line-height: 0.95;
      margin: 0;
    }}
    .updated {{
      color: var(--muted); font-size: 0.78rem; white-space: nowrap;
    }}
    .hero {{
      position: relative;
      overflow: hidden;
      border-radius: calc(var(--radius) + 6px);
      padding: 22px 18px 20px;
      background:
        linear-gradient(135deg, rgba(31,107,87,0.95), rgba(20,55,48,0.92) 55%, rgba(196,122,18,0.88));
      color: #f7f3ea;
      box-shadow: var(--shadow);
      isolation: isolate;
    }}
    .hero::before {{
      content: "";
      position: absolute; inset: 0;
      background:
        repeating-linear-gradient(
          90deg,
          transparent 0 18px,
          rgba(255,255,255,0.05) 18px 19px
        );
      opacity: 0.35;
      z-index: -1;
    }}
    .route-kicker {{
      font-size: 0.85rem; opacity: 0.9; margin: 0 0 8px;
      letter-spacing: 0.04em; text-transform: uppercase;
    }}
    .status-pill {{
      display: inline-flex; align-items: center; gap: 8px;
      font-family: "Fraunces", Georgia, serif;
      font-size: clamp(1.55rem, 6.5vw, 2.1rem);
      font-weight: 700; margin: 0; letter-spacing: -0.02em;
    }}
    .dot {{
      width: 12px; height: 12px; border-radius: 50%;
      background: #f5d76e; box-shadow: 0 0 0 0 rgba(245,215,110,0.7);
      animation: pulse 1.8s ease-out infinite;
    }}
    .status-critical .dot {{ background: #ff7b72; }}
    .status-warning .dot {{ background: #ffb020; }}
    .status-clear .dot {{ background: #6dffb0; }}
    @keyframes pulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(255,255,255,0.45); }}
      70% {{ box-shadow: 0 0 0 14px rgba(255,255,255,0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(255,255,255,0); }}
    }}
    .hint {{ margin: 10px 0 0; opacity: 0.92; font-size: 0.98rem; line-height: 1.4; max-width: 34ch; }}
    .metrics {{
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 10px;
      margin-top: 16px;
    }}
    .metric {{
      background: rgba(255,255,255,0.12);
      border: 1px solid rgba(255,255,255,0.18);
      border-radius: 16px;
      padding: 12px;
    }}
    .metric span {{ display: block; font-size: 0.75rem; opacity: 0.8; }}
    .metric strong {{
      display: block; margin-top: 4px;
      font-family: "Fraunces", Georgia, serif;
      font-size: 1.25rem; font-weight: 700;
    }}
    section {{
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid rgba(215,199,161,0.65);
      border-radius: var(--radius);
      padding: 16px;
      backdrop-filter: blur(8px);
    }}
    h2 {{
      font-family: "Fraunces", Georgia, serif;
      font-size: 1.25rem; margin: 0 0 12px; letter-spacing: -0.02em;
    }}
    h3 {{
      font-family: "Fraunces", Georgia, serif;
      font-size: 1.05rem; margin: 18px 0 10px; letter-spacing: -0.02em;
    }}

    /* Perfect departure */
    .perfect {{
      padding: 0;
      overflow: hidden;
      border: 0;
      background: transparent;
    }}
    .perfect-head {{
      display: flex; align-items: baseline; justify-content: space-between; gap: 10px;
      margin-bottom: 10px; padding: 0 2px;
    }}
    .perfect-head h2 {{ margin: 0; }}
    .perfect-stamp {{ color: var(--muted); font-size: 0.75rem; font-weight: 600; }}
    .perfect-hero {{
      position: relative;
      border-radius: calc(var(--radius) + 4px);
      padding: 22px 18px 18px;
      color: #f8f4ea;
      background:
        radial-gradient(800px 280px at 90% -20%, rgba(245, 196, 110, 0.35), transparent 55%),
        linear-gradient(160deg, #163a32 0%, #1f5c4a 48%, #8a5a12 120%);
      box-shadow: var(--shadow);
      overflow: hidden;
      isolation: isolate;
    }}
    .perfect-hero::after {{
      content: "";
      position: absolute;
      inset: auto -20% -40% 40%;
      height: 180px;
      background: radial-gradient(circle, rgba(255,255,255,0.12), transparent 65%);
      animation: drift 8s ease-in-out infinite alternate;
      z-index: -1;
    }}
    @keyframes drift {{
      from {{ transform: translateX(0); }}
      to {{ transform: translateX(-30px); }}
    }}
    .perfect-kicker {{
      margin: 0;
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      opacity: 0.85;
    }}
    .perfect-time {{
      margin: 6px 0 0;
      font-family: "Fraunces", Georgia, serif;
      font-size: clamp(3.4rem, 16vw, 4.8rem);
      line-height: 0.92;
      letter-spacing: -0.04em;
      font-weight: 700;
      animation: rise 0.7s ease both;
    }}
    .perfect-day {{
      margin: 8px 0 0;
      font-weight: 700;
      opacity: 0.92;
    }}
    .journey {{
      list-style: none;
      margin: 18px 0 0;
      padding: 14px 12px;
      display: grid;
      grid-template-columns: 1fr 18px 1fr 18px 1fr;
      gap: 6px;
      align-items: center;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.14);
      border-radius: 18px;
    }}
    .journey li {{ text-align: center; }}
    .journey .j-label {{
      display: block;
      font-size: 0.68rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      opacity: 0.75;
    }}
    .journey strong {{
      display: block;
      margin-top: 2px;
      font-family: "Fraunces", Georgia, serif;
      font-size: 1.35rem;
    }}
    .journey .j-sub {{
      display: block;
      margin-top: 2px;
      font-size: 0.72rem;
      opacity: 0.8;
      line-height: 1.25;
    }}
    .j-line {{
      height: 2px;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,0.7), transparent);
      border-radius: 2px;
      animation: shimmer 2.4s ease-in-out infinite;
    }}
    @keyframes shimmer {{
      0%, 100% {{ opacity: 0.35; }}
      50% {{ opacity: 1; }}
    }}
    .perfect-stats {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      margin-top: 14px;
    }}
    .perfect-stats div {{
      background: rgba(0,0,0,0.15);
      border-radius: 14px;
      padding: 10px 8px;
      text-align: center;
    }}
    .perfect-stats span {{ display: block; font-size: 0.7rem; opacity: 0.75; }}
    .perfect-stats strong {{
      display: block; margin-top: 3px;
      font-family: "Fraunces", Georgia, serif; font-size: 1.05rem;
    }}
    .perfect-route {{
      margin: 14px 0 0;
      font-size: 0.86rem;
      line-height: 1.4;
      opacity: 0.9;
    }}
    .maps-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      margin-top: 14px;
      min-height: 48px;
      padding: 12px 18px;
      border-radius: 999px;
      background: #f4e4b8;
      color: #17352d;
      font-weight: 800;
      text-decoration: none;
      box-shadow: 0 8px 24px rgba(0,0,0,0.18);
      transition: transform 0.2s ease;
    }}
    .maps-btn:hover {{ transform: translateY(-1px); }}
    .maps-btn.ghost {{
      background: transparent;
      color: var(--ink);
      border: 1px solid var(--line);
      box-shadow: none;
      margin-top: 10px;
    }}
    .perfect-note {{
      margin: 12px 0 0;
      font-size: 0.75rem;
      opacity: 0.78;
      line-height: 1.35;
    }}
    .perfect-alt {{
      margin-top: 12px;
      background: var(--panel);
      border: 1px solid rgba(215,199,161,0.65);
      border-radius: var(--radius);
      padding: 14px 16px;
    }}
    .alt-kicker {{
      font-size: 0.75rem;
      font-weight: 800;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--accent-2);
      margin-bottom: 8px;
    }}
    .alt-grid {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
    }}
    .alt-grid span {{ display: block; color: var(--muted); font-size: 0.72rem; }}
    .alt-grid strong {{
      display: block; margin-top: 2px;
      font-family: "Fraunces", Georgia, serif; font-size: 1.05rem;
    }}
    .perfect-timeline-wrap, .perfect-slots-wrap {{
      margin-top: 12px;
      background: var(--panel);
      border: 1px solid rgba(215,199,161,0.65);
      border-radius: var(--radius);
      padding: 14px 16px 16px;
    }}
    .perfect-timeline-wrap h3, .perfect-slots-wrap h3 {{ margin-top: 0; }}
    .timeline {{
      list-style: none;
      margin: 0;
      padding: 4px 2px 8px;
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: minmax(92px, 1fr);
      gap: 8px;
      overflow-x: auto;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
    }}
    .tl-item {{
      scroll-snap-align: start;
      background: #fffaf0;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 10px 8px;
      text-align: center;
      min-height: 96px;
      opacity: 0;
      transform: translateY(8px);
      animation: rise 0.5s ease forwards;
    }}
    .tl-item.best {{
      border-color: var(--accent-2);
      box-shadow: 0 0 0 2px rgba(31,107,87,0.18);
      background: #e8f3ee;
    }}
    .tl-item.night {{
      background: #eef2f6;
      border-color: #c5d0da;
    }}
    .tl-item.night.best {{
      background: #e8f3ee;
      border-color: var(--accent-2);
    }}
    .tl-depart {{ display: block; font-weight: 800; font-size: 0.92rem; }}
    .tl-bar {{
      display: block;
      height: 6px;
      border-radius: 999px;
      margin: 8px auto;
      width: 70%;
      background: #c8d9d1;
    }}
    .tl-item.load-frei .tl-bar {{ background: linear-gradient(90deg, #1f6b57, #6dffb0); }}
    .tl-item.load-ok .tl-bar {{ background: linear-gradient(90deg, #c47a12, #f0c56d); }}
    .tl-item.load-voll .tl-bar {{ background: linear-gradient(90deg, #b42318, #ff8f86); }}
    .tl-meta {{ display: block; font-size: 0.68rem; color: var(--muted); line-height: 1.3; }}
    .slot-list {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }}
    .slot-row {{
      display: grid;
      grid-template-columns: 72px 1fr 64px;
      gap: 8px;
      align-items: center;
      padding: 10px 8px;
      border-bottom: 1px dashed var(--line);
    }}
    .slot-row:last-child {{ border-bottom: 0; }}
    .slot-when strong, .slot-arrive strong {{
      display: block;
      font-family: "Fraunces", Georgia, serif;
      font-size: 1.15rem;
    }}
    .slot-when span, .slot-arrive span, .slot-path span {{
      display: block;
      color: var(--muted);
      font-size: 0.72rem;
      line-height: 1.3;
    }}
    .slot-arrive {{ text-align: right; }}
    .perfect-empty {{
      background: var(--panel);
      border: 1px solid rgba(215,199,161,0.65);
      border-radius: var(--radius);
      padding: 16px;
    }}

    .stops {{
      list-style: none; margin: 0; padding: 0;
      display: grid; gap: 0;
    }}
    .stops li {{
      display: grid;
      grid-template-columns: 16px 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px dashed var(--line);
      opacity: 0; transform: translateY(8px);
      animation: rise 0.55s ease forwards;
    }}
    .stops li:last-child {{ border-bottom: 0; }}
    .stops li::before {{
      content: "";
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--accent-2);
      box-shadow: 0 0 0 4px rgba(31,107,87,0.12);
      justify-self: center;
    }}
    .stop-name {{ font-weight: 700; }}
    .stop-tag {{
      color: var(--muted); font-size: 0.78rem; font-weight: 600;
    }}
    .alert {{
      padding: 12px 0;
      border-bottom: 1px solid rgba(215,199,161,0.55);
      opacity: 0; transform: translateY(10px);
      animation: rise 0.6s ease forwards;
    }}
    .alert:last-child {{ border-bottom: 0; }}
    .alert-top {{
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
      margin-bottom: 6px;
    }}
    .badge {{
      font-size: 0.7rem; font-weight: 700; letter-spacing: 0.04em;
      text-transform: uppercase;
      padding: 4px 8px; border-radius: 999px;
      background: #ece7da; color: var(--muted);
    }}
    .badge.critical {{ background: #f8d7d4; color: var(--critical); }}
    .badge.warning {{ background: #fce7c8; color: var(--warning); }}
    .badge.info {{ background: #ddece7; color: var(--clear); }}
    .alert h3 {{
      margin: 0 0 4px; font-size: 1.02rem; line-height: 1.3;
    }}
    .alert p {{
      margin: 0; color: var(--muted); font-size: 0.92rem; line-height: 1.4;
    }}
    .alert a {{ color: var(--accent-2); font-weight: 700; font-size: 0.85rem; }}
    .checklist, .downs {{
      margin: 0; padding-left: 1.1rem; color: var(--muted); line-height: 1.55;
    }}
    .links {{
      display: flex; flex-wrap: wrap; gap: 8px;
    }}
    .links a {{
      text-decoration: none;
      color: var(--ink);
      background: #fffaf0;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 10px 14px;
      font-weight: 700; font-size: 0.88rem;
      min-height: 44px; display: inline-flex; align-items: center;
    }}
    .empty {{ color: var(--muted); margin: 0; }}
    .cams {{ display: grid; grid-template-columns: 1fr; gap: 14px; }}
    @media (min-width: 560px) {{ .cams {{ grid-template-columns: 1fr 1fr; }} }}
    .cam {{
      border: 1px solid var(--line); border-radius: 16px; overflow: hidden;
      background: #fffaf0;
      padding: 0; margin: 0; width: 100%;
      text-align: left; font: inherit; color: inherit;
      cursor: pointer; -webkit-tap-highlight-color: transparent;
      appearance: none; display: block;
      transition: transform 0.15s ease, box-shadow 0.15s ease;
    }}
    .cam:active {{ transform: scale(0.985); }}
    .cam:focus-visible {{ outline: 3px solid var(--accent-2); outline-offset: 2px; }}
    .cam.relevant {{ border-color: var(--accent-2); box-shadow: 0 0 0 2px rgba(31,107,87,0.18); }}
    .cam-media {{ position: relative; }}
    .cam img {{ display: block; width: 100%; height: auto; background: #dfe6e2; }}
    .cam-zoom {{
      position: absolute; right: 10px; bottom: 10px;
      background: rgba(20,35,31,0.72); color: #f7f3ea;
      font-size: 0.72rem; font-weight: 700; letter-spacing: 0.02em;
      padding: 6px 10px; border-radius: 999px;
      pointer-events: none;
    }}
    .cam-body {{ padding: 10px 12px; }}
    .cam-name {{ font-weight: 700; font-size: 0.92rem; margin: 0 0 4px; }}
    .cam-meta {{ color: var(--muted); font-size: 0.8rem; margin: 0; line-height: 1.35; }}
    .cam-sev {{
      display: inline-block; font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.04em; padding: 3px 7px; border-radius: 999px; margin-bottom: 4px;
      background: #ece7da; color: var(--muted);
    }}
    .cam-sev.critical {{ background: #f8d7d4; color: var(--critical); }}
    .cam-sev.warning {{ background: #fce7c8; color: var(--warning); }}
    .cam-sev.clear {{ background: #ddece7; color: var(--clear); }}
    .border-now {{
      margin-top: 18px;
      border-radius: calc(var(--radius) + 4px);
      padding: 18px 16px;
      background:
        radial-gradient(700px 240px at 100% 0%, rgba(196,122,18,0.18), transparent 55%),
        linear-gradient(160deg, #fffaf0 0%, #f3efe4 100%);
      border: 1px solid rgba(215,199,161,0.75);
      box-shadow: var(--shadow);
    }}
    .border-now h2 {{ margin-bottom: 6px; }}
    .border-sides {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 14px;
    }}
    .border-side {{
      background: rgba(255,255,255,0.65);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px 12px;
    }}
    .side-kicker {{
      display: block;
      color: var(--muted);
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 6px;
    }}
    .side-cars {{
      display: block;
      font-family: "Fraunces", Georgia, serif;
      font-size: clamp(1.55rem, 6vw, 2.1rem);
      letter-spacing: -0.03em;
      line-height: 1.1;
    }}
    .side-cars small {{
      font-family: "Manrope", system-ui, sans-serif;
      font-size: 0.72rem;
      font-weight: 700;
      color: var(--muted);
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }}
    .side-meta {{
      display: block;
      margin-top: 8px;
      color: var(--ink);
      font-size: 0.92rem;
      font-weight: 600;
    }}
    .side-note {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.8rem;
      line-height: 1.35;
    }}
    .border-note {{ margin: 10px 0 0; color: var(--muted); font-size: 0.86rem; line-height: 1.4; }}
    .stale-tag {{ color: var(--warning); font-size: 0.72rem; font-weight: 800; }}
    @media (max-width: 520px) {{
      .border-sides {{ grid-template-columns: 1fr; }}
    }}
    .lightbox {{
      position: fixed; inset: 0; z-index: 1000;
      display: none; align-items: center; justify-content: center;
      padding: max(12px, env(safe-area-inset-top)) 12px max(12px, env(safe-area-inset-bottom));
      background: rgba(10, 18, 16, 0.92);
      backdrop-filter: blur(6px);
    }}
    .lightbox.open {{ display: flex; }}
    .lightbox-inner {{
      width: min(960px, 100%);
      max-height: 100%;
      display: flex; flex-direction: column; gap: 10px;
    }}
    .lightbox img {{
      width: 100%; height: auto; max-height: min(78vh, 820px);
      object-fit: contain; border-radius: 14px;
      background: #111; box-shadow: 0 16px 40px rgba(0,0,0,0.35);
    }}
    .lightbox-caption {{
      color: #f3efe4; font-weight: 700; font-size: 0.95rem;
      text-align: center; line-height: 1.35;
    }}
    .lightbox-close {{
      align-self: flex-end;
      min-height: 44px; min-width: 44px;
      border: 0; border-radius: 999px;
      background: #f4e4b8; color: #17352d;
      font-weight: 800; font-size: 1rem;
      cursor: pointer; padding: 10px 16px;
    }}
    footer {{
      margin-top: 18px; color: var(--muted); font-size: 0.78rem; text-align: center;
    }}
    @keyframes rise {{
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .stops li:nth-child(1) {{ animation-delay: 0.05s; }}
    .stops li:nth-child(2) {{ animation-delay: 0.1s; }}
    .stops li:nth-child(3) {{ animation-delay: 0.15s; }}
    .stops li:nth-child(4) {{ animation-delay: 0.2s; }}
    .stops li:nth-child(5) {{ animation-delay: 0.25s; }}
    .stops li:nth-child(6) {{ animation-delay: 0.3s; }}
    .stops li:nth-child(7) {{ animation-delay: 0.35s; }}
    .tl-item:nth-child(1) {{ animation-delay: 0.05s; }}
    .tl-item:nth-child(2) {{ animation-delay: 0.1s; }}
    .tl-item:nth-child(3) {{ animation-delay: 0.15s; }}
    .tl-item:nth-child(4) {{ animation-delay: 0.2s; }}
    .tl-item:nth-child(5) {{ animation-delay: 0.25s; }}
    .tl-item:nth-child(6) {{ animation-delay: 0.3s; }}
    .alert:nth-child(1) {{ animation-delay: 0.08s; }}
    .alert:nth-child(2) {{ animation-delay: 0.14s; }}
    .alert:nth-child(3) {{ animation-delay: 0.2s; }}
    .alert:nth-child(4) {{ animation-delay: 0.26s; }}
    .alert:nth-child(5) {{ animation-delay: 0.32s; }}
    @media (min-width: 720px) {{
      .wrap {{ padding-top: 28px; }}
      .metrics {{ grid-template-columns: 1.4fr 1fr 1fr; }}
    }}
    @media (max-width: 420px) {{
      .journey {{ grid-template-columns: 1fr; gap: 10px; }}
      .j-line {{ height: 18px; width: 2px; margin: 0 auto; background: linear-gradient(180deg, transparent, rgba(255,255,255,0.7), transparent); }}
      .slot-row {{ grid-template-columns: 64px 1fr 56px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .dot, .stops li, .alert, .tl-item, .perfect-time, .perfect-hero::after, .j-line {{
        animation: none !important; opacity: 1; transform: none;
      }}
    }}
  </style>
</head>
<body class="status-{html.escape(payload["status"])}">
  <main class="wrap">
    <div class="brand-row">
      <h1 class="brand">{html.escape(payload["brand"])}</h1>
      <div class="updated">Update<br/>{html.escape(payload["generated_label"])}</div>
    </div>

    <header class="hero status-{html.escape(payload["status"])}">
      <p class="route-kicker">{html.escape(payload["from"])} → {html.escape(payload["to"])}</p>
      <p class="status-pill"><span class="dot" aria-hidden="true"></span>{html.escape(payload["status_label"])}</p>
      <p class="hint">{html.escape(payload["status_hint"])}</p>
      <div class="metrics">
        <div class="metric">
          <span>Einfahrt BiH</span>
          <strong>{html.escape(hero_bih)}</strong>
        </div>
        <div class="metric">
          <span>Einfahrt HR</span>
          <strong>{html.escape(hero_hr)}</strong>
        </div>
        <div class="metric">
          <span>Perfekte Abfahrt</span>
          <strong id="hero-depart">{html.escape(hero_depart)}</strong>
        </div>
      </div>
    </header>

    {border_html}

    {perfect_html}

    {"<section aria-labelledby='cams-title'><h2 id='cams-title'>Grenz-Kameras (HAK, live)</h2><p class='empty' style='margin-bottom:12px'>Maljevac / Velika Kladuša · Bild aktualisiert ~alle 60s · KI-Auswertung fließt in Grenzwartezeit &amp; Routing ein</p><div class='cams'>" + cameras_html + "</div></section>" if cameras_html else ""}

    <section aria-labelledby="route-title">
      <h2 id="route-title">Route</h2>
      <ol class="stops">{stops_html}</ol>
    </section>

    <section aria-labelledby="alerts-title">
      <h2 id="alerts-title">Live-Meldungen</h2>
      <div class="alerts">{alerts_html}</div>
    </section>

    <section aria-labelledby="check-title">
      <h2 id="check-title">Vor dem Losfahren</h2>
      <ul class="checklist">{checks_html}</ul>
    </section>

    <section aria-labelledby="links-title">
      <h2 id="links-title">Live-Tools</h2>
      <div class="links">{links_html}</div>
    </section>

    {"<section><h2>Quellen offline</h2><ul class='downs'>" + downs_html + "</ul></section>" if downs_html else ""}

    <footer>BuzimLine · Auto-Refresh alle 15 Min · Perfect + HAK-Kameras + optional OpenAI Vision</footer>
  </main>
  <div class="lightbox" id="cam-lightbox" hidden aria-hidden="true" role="dialog" aria-modal="true" aria-label="Kamera vergrößert">
    <div class="lightbox-inner">
      <button type="button" class="lightbox-close" id="cam-lightbox-close" aria-label="Schließen">Schließen</button>
      <img id="cam-lightbox-img" alt="" />
      <div class="lightbox-caption" id="cam-lightbox-caption"></div>
    </div>
  </div>
  <script type="application/json" id="payload">{payload_json}</script>
  <script>
    const mins = 15;
    setTimeout(() => location.reload(), mins * 60 * 1000);
    // Soft live refresh of HAK camera stills
    setInterval(() => {{
      document.querySelectorAll('img[data-cam]').forEach((img) => {{
        const base = img.dataset.cam;
        if (!base) return;
        const sep = base.includes('?') ? '&' : '?';
        img.src = base + sep + 't=' + Date.now();
      }});
      const lb = document.getElementById('cam-lightbox-img');
      if (lb && lb.dataset.cam && document.getElementById('cam-lightbox').classList.contains('open')) {{
        const base = lb.dataset.cam;
        const sep = base.includes('?') ? '&' : '?';
        lb.src = base + sep + 't=' + Date.now();
      }}
    }}, 60000);

    (function cameraLightbox() {{
      const box = document.getElementById('cam-lightbox');
      const img = document.getElementById('cam-lightbox-img');
      const caption = document.getElementById('cam-lightbox-caption');
      const closeBtn = document.getElementById('cam-lightbox-close');
      if (!box || !img) return;

      function openCam(btn) {{
        const thumb = btn.querySelector('img[data-cam]');
        if (!thumb) return;
        const live = thumb.dataset.cam || thumb.src;
        const sep = live.includes('?') ? '&' : '?';
        img.src = live + sep + 't=' + Date.now();
        img.dataset.cam = live;
        img.alt = thumb.alt || 'Kamera';
        caption.textContent = btn.dataset.camTitle || thumb.alt || '';
        box.hidden = false;
        box.classList.add('open');
        box.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
        closeBtn.focus();
      }}
      function closeCam() {{
        box.classList.remove('open');
        box.hidden = true;
        box.setAttribute('aria-hidden', 'true');
        document.body.style.overflow = '';
        img.removeAttribute('src');
      }}

      document.querySelectorAll('button.cam[data-cam-open]').forEach((btn) => {{
        btn.addEventListener('click', () => openCam(btn));
      }});
      closeBtn.addEventListener('click', closeCam);
      box.addEventListener('click', (e) => {{ if (e.target === box) closeCam(); }});
      document.addEventListener('keydown', (e) => {{
        if (e.key === 'Escape' && box.classList.contains('open')) closeCam();
      }});
    }})();

    // If recommended departure is missed, roll to next-best from cached slots
    (function advancePerfectLive() {{
      const raw = document.getElementById('payload');
      if (!raw) return;
      let data;
      try {{ data = JSON.parse(raw.textContent); }} catch (_) {{ return; }}
      const perfect = data && data.perfect;
      if (!perfect || !perfect.best) return;
      const graceMs = 5 * 60 * 1000;

      function upcoming(slots) {{
        const now = Date.now();
        return (slots || []).filter((s) => {{
          const t = Date.parse(s.depart);
          return !Number.isNaN(t) && t > now - graceMs;
        }});
      }}

      function pickBest() {{
        const pool = upcoming(perfect.top);
        if (!pool.length) return null;
        pool.sort((a, b) => {{
          const aa = Date.parse(a.arrive_buzim || a.depart);
          const bb = Date.parse(b.arrive_buzim || b.depart);
          return aa - bb;
        }});
        return pool[0];
      }}

      function apply(best) {{
        const set = (id, text) => {{
          const el = document.getElementById(id);
          if (el && text != null) el.textContent = text;
        }};
        set('perfect-time', best.depart_short);
        set('perfect-day', (best.depart_day || '') + ' · ' + (best.badge || 'nächste beste'));
        set('perfect-los', best.depart_short);
        set('perfect-border', best.arrive_border_short);
        if (best.border_wait_min != null && best.border_cars != null) {{
          set('perfect-border-meta', '~' + best.border_wait_min + ' min · ' + best.border_cars + ' Autos');
        }}
        set('perfect-arrive', best.arrive_buzim_short);
        set('perfect-total', best.total_label);
        if (best.distance_km != null) set('perfect-dist', '~' + best.distance_km + ' km');
        set('perfect-route-id', best.route_id);
        const maps = document.getElementById('perfect-maps');
        if (maps && best.maps_url) maps.href = best.maps_url;
        set('hero-depart', best.depart_label || best.depart_short);
        document.querySelectorAll('.tl-item').forEach((li) => {{
          const label = (li.querySelector('.tl-depart') || {{}}).textContent || '';
          const hit = label.indexOf(best.depart_short) !== -1;
          li.classList.toggle('best', hit);
        }});
      }}

      function tick() {{
        const next = pickBest();
        if (!next) return;
        if (next.depart !== perfect.best.depart) {{
          next.badge = 'nächste beste';
          perfect.best = next;
          apply(next);
        }}
      }}
      tick();
      setInterval(tick, 30000);
    }})();
  </script>
</body>
</html>
"""


def _border_section(maljevac_now: dict | None, borders: list[dict]) -> str:
    if not maljevac_now:
        return """
    <section class="border-now" aria-labelledby="border-title">
      <h2 id="border-title">Maljevac jetzt (HAK-Kamera)</h2>
      <p class="empty">Noch keine KI-Zählung. Nächster Monitor-Lauf wertet Cam 430 (→BiH) und 429 (→HR) per gpt-4o-mini aus.</p>
    </section>
    """

    def side_card(title: str, side: dict | None) -> str:
        if not side:
            return f"""
            <div class="border-side">
              <span class="side-kicker">{html.escape(title)}</span>
              <strong class="side-cars">—</strong>
              <span class="side-meta">noch keine Zählung</span>
            </div>
            """
        cars = side.get("cars")
        wait = side.get("wait_min")
        trucks = side.get("trucks")
        cars_l = "—" if cars is None else str(cars)
        wait_l = "—" if wait is None else f"~{wait} min"
        trucks_l = "" if trucks is None else f" · ~{trucks} LKW"
        end_l = "" if side.get("queue_end_visible") is not False else " · Ende nicht sichtbar"
        note = html.escape((side.get("note") or "")[:110])
        return f"""
            <div class="border-side">
              <span class="side-kicker">{html.escape(title)}</span>
              <strong class="side-cars">{html.escape(cars_l)} <small>Autos</small></strong>
              <span class="side-meta">Wartezeit {html.escape(wait_l)}{html.escape(trucks_l)}{html.escape(end_l)}</span>
              {"<span class='side-note'>" + note + "</span>" if note else ""}
            </div>
            """

    to_bih = maljevac_now.get("to_bih")
    to_hr = maljevac_now.get("to_hr")
    source = html.escape(str(maljevac_now.get("source") or "HAK-Cam"))

    # Optional Nakordoni reference row (not primary)
    ref = ""
    for b in borders[:3]:
        if "maljevac" not in (b.get("name") or "").lower():
            continue
        stale = " · veraltet" if b.get("stale") else ""
        cars_b = b["cars"] if b.get("cars") is not None else "—"
        wait_b = f"~{b['wait_min']} min" if b.get("wait_min") is not None else "—"
        ref = (
            f"<p class='border-note'>Nakordoni Referenz: {cars_b} Autos / {wait_b}{stale} "
            f"(nicht führend — Anzeige kommt von HAK-Kamera)</p>"
        )
        break

    return f"""
    <section class="border-now" aria-labelledby="border-title">
      <h2 id="border-title">Maljevac jetzt</h2>
      <p class="empty" style="margin:0">Live gezählt von HAK-Kameras · {source}</p>
      <div class="border-sides">
        {side_card("Einfahrt BiH (HR → BiH)", to_bih)}
        {side_card("Einfahrt HR (BiH → HR)", to_hr)}
      </div>
      {ref}
    </section>
    """


def _camera_card(cam: dict) -> str:
    name = html.escape(cam.get("name") or "")
    direction = html.escape(cam.get("direction") or "")
    img = html.escape(cam.get("image_url") or "")
    live = html.escape(cam.get("live_url") or cam.get("image_url") or "")
    relevant = " relevant" if cam.get("relevant") else ""
    severity = cam.get("severity")
    sev_label = "frei" if severity == "info" else (severity or "")
    sev_html = (
        f'<span class="cam-sev {html.escape(severity)}">{html.escape(sev_label)}</span><br/>'
        if severity
        else '<span class="cam-sev">Live</span><br/>'
    )
    meta_bits = []
    if direction:
        meta_bits.append(direction)
    if cam.get("wait_min") is not None:
        meta_bits.append(f"KI-Wartezeit ~{cam['wait_min']} min")
    if cam.get("vehicles") is not None:
        meta_bits.append(f"~{cam['vehicles']} Autos")
    if cam.get("trucks") is not None:
        meta_bits.append(f"~{cam['trucks']} LKW")
    if cam.get("queue_end_visible") is False:
        meta_bits.append("Ende nicht sichtbar")
    if cam.get("weather"):
        meta_bits.append(str(cam["weather"]))
    if cam.get("road"):
        meta_bits.append(str(cam["road"]))
    meta = html.escape(" · ".join(meta_bits))
    verdict = html.escape((cam.get("verdict") or "")[:160])
    verdict_html = (
        f'<span class="cam-meta" style="display:block;margin-top:4px">{verdict}</span>'
        if verdict
        else ""
    )
    return f"""
    <button type="button" class="cam{relevant}" data-cam-open data-cam-title="{name}" aria-label="Kamera vergrößern: {name}">
      <div class="cam-media">
        <img src="{img}" data-cam="{live}" alt="HAK Kamera {name}" loading="lazy" />
        <span class="cam-zoom">Tippen · größer</span>
      </div>
      <span class="cam-body">
        {sev_html}
        <span class="cam-name" style="display:block">{name}</span>
        <span class="cam-meta" style="display:block">{meta}</span>
        {verdict_html}
      </span>
    </button>
    """


def _alert_row(alert: dict) -> str:
    delay = f' · ~{alert["delay_min"]} min' if alert.get("delay_min") is not None else ""
    loc = html.escape(alert.get("location") or "")
    link = (
        f'<p><a href="{html.escape(alert["url"])}" target="_blank" rel="noopener">Quelle öffnen</a></p>'
        if alert.get("url")
        else ""
    )
    return f"""
    <article class="alert">
      <div class="alert-top">
        <span class="badge {html.escape(alert["severity"])}">{html.escape(alert["severity"])}</span>
        <span class="badge">{html.escape(alert["source"])}{html.escape(delay)}</span>
        {"<span class='badge'>" + loc + "</span>" if loc else ""}
      </div>
      <h3>{html.escape(alert["title"])}</h3>
      <p>{html.escape((alert.get("detail") or "")[:220])}</p>
      {link}
    </article>
    """


def write_dashboard(
    out_dir: str | Path = "site",
    config_path: str | None = None,
) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    config = load_config(config_path)
    alerts = fetch_all(config)
    perfect = load_perfect_json(out / "perfect.json")
    payload = _payload_from_alerts(config, alerts, perfect=perfect)
    # Snapshot live JPEGs so GitHub Pages always shows a fresh still
    snaps = snapshot_cameras(config, out)
    if snaps:
        for cam in payload.get("cameras") or []:
            local = snaps.get(cam.get("id"))
            if local:
                cam["image_url"] = local
    html_path = out / "index.html"
    html_path.write_text(render_html(payload), encoding="utf-8")
    (out / "status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]Dashboard geschrieben:[/green] {html_path}")
    if snaps:
        console.print(f"[green]Kamera-Snapshots:[/green] {len(snaps)} Stück in {out / 'cams'}")
    return html_path


def serve_dashboard(out_dir: str | Path = "site", host: str = "0.0.0.0", port: int = 8080) -> None:
    root = Path(out_dir).resolve()
    if not (root / "index.html").exists():
        write_dashboard(root)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

    httpd = ThreadingHTTPServer((host, port), Handler)
    console.print(f"[bold]Dashboard:[/bold] http://{host}:{port}/")
    httpd.serve_forever()
