from __future__ import annotations

import html
import json
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rich.console import Console

from traffic_monitor.config import load_config
from traffic_monitor.models import Alert
from traffic_monitor.recommend import TZ, _score, recommend_departure
from traffic_monitor.sources import fetch_all
from traffic_monitor.sources.hak_cameras import cameras_from_config

console = Console()

ROUTE_STOPS = [
    ("Stuttgart", "Start"),
    ("Salzburg", "AT"),
    ("Tauern", "A10"),
    ("Karawanken", "A11"),
    ("Zagreb", "HR"),
    ("Maljevac", "Grenze"),
    ("Bužim", "Ziel"),
]


def build_dashboard_payload(config_path: str | None = None) -> dict:
    config = load_config(config_path)
    return _payload_from_alerts(config, fetch_all(config))


def _payload_from_alerts(config: dict, alerts: list[Alert]) -> dict:
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

    cam_verdicts = {
        a.location: a
        for a in alerts
        if a.source == "HAK-Cam"
    }
    cameras = []
    for cam in cameras_from_config(config):
        verdict = cam_verdicts.get(cam["name"])
        cameras.append(
            {
                **cam,
                "severity": verdict.severity if verdict else None,
                "verdict": verdict.detail if verdict else None,
                "wait_min": verdict.delay_min if verdict else None,
            }
        )

    return {
        "generated_at": now.isoformat(),
        "generated_label": now.strftime("%d.%m.%Y %H:%M"),
        "tz": "Europe/Berlin",
        "brand": "BuzimLine",
        "from": route.get("from", "Stuttgart"),
        "to": route.get("to", "Bužim"),
        "via": route.get("via", ""),
        "approx_km": route.get("approx_km", 930),
        "status": status,
        "status_label": status_label,
        "status_hint": status_hint,
        "counts": {"critical": critical_n, "warning": warning_n, "total": len(alerts)},
        "best_departure": best,
        "stops": ROUTE_STOPS,
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
    payload_json = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)

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
    .hint {{ margin: 10px 0 0; opacity: 0.92; font-size: 0.98rem; line-height: 1.4; max-width: 30ch; }}
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
    }}
    .cam.relevant {{ border-color: var(--accent-2); box-shadow: 0 0 0 2px rgba(31,107,87,0.18); }}
    .cam img {{ display: block; width: 100%; height: auto; background: #dfe6e2; }}
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
    .alert:nth-child(1) {{ animation-delay: 0.08s; }}
    .alert:nth-child(2) {{ animation-delay: 0.14s; }}
    .alert:nth-child(3) {{ animation-delay: 0.2s; }}
    .alert:nth-child(4) {{ animation-delay: 0.26s; }}
    .alert:nth-child(5) {{ animation-delay: 0.32s; }}
    @media (min-width: 720px) {{
      .wrap {{ padding-top: 28px; }}
      .metrics {{ grid-template-columns: 1.4fr 1fr 1fr; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .dot, .stops li, .alert {{ animation: none !important; opacity: 1; transform: none; }}
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
          <span>Beste Abfahrt</span>
          <strong>{html.escape(payload["best_departure"]["when"])}</strong>
        </div>
        <div class="metric">
          <span>Distanz</span>
          <strong>~{payload["approx_km"]} km</strong>
        </div>
        <div class="metric">
          <span>Live-Treffer</span>
          <strong>{payload["counts"]["critical"]} krit. / {payload["counts"]["warning"]} warn.</strong>
        </div>
      </div>
    </header>

    <section aria-labelledby="route-title">
      <h2 id="route-title">Route</h2>
      <ol class="stops">{stops_html}</ol>
    </section>

    <section aria-labelledby="alerts-title">
      <h2 id="alerts-title">Live-Meldungen</h2>
      <div class="alerts">{alerts_html}</div>
    </section>

    {"<section aria-labelledby='cams-title'><h2 id='cams-title'>Grenz-Kameras (HAK, live)</h2><div class='cams'>" + cameras_html + "</div></section>" if cameras_html else ""}

    <section aria-labelledby="check-title">
      <h2 id="check-title">Vor dem Losfahren</h2>
      <ul class="checklist">{checks_html}</ul>
    </section>

    <section aria-labelledby="links-title">
      <h2 id="links-title">Live-Tools</h2>
      <div class="links">{links_html}</div>
    </section>

    {"<section><h2>Quellen offline</h2><ul class='downs'>" + downs_html + "</ul></section>" if downs_html else ""}

    <footer>BuzimLine · Auto-Refresh alle 15 Min über GitHub Actions · Daten ohne Gewähr</footer>
  </main>
  <script type="application/json" id="payload">{payload_json}</script>
  <script>
    // Soft auto-reload so phones stay fresh when the tab is open
    const mins = 15;
    setTimeout(() => location.reload(), mins * 60 * 1000);
  </script>
</body>
</html>
"""


def _camera_card(cam: dict) -> str:
    name = html.escape(cam.get("name") or "")
    direction = html.escape(cam.get("direction") or "")
    img = html.escape(cam.get("image_url") or "")
    relevant = " relevant" if cam.get("relevant") else ""
    severity = cam.get("severity")
    sev_html = (
        f'<span class="cam-sev {html.escape(severity)}">{html.escape(severity)}</span><br/>'
        if severity
        else ""
    )
    wait = cam.get("wait_min")
    meta_bits = []
    if direction:
        meta_bits.append(direction)
    if wait is not None:
        meta_bits.append(f"KI-Wartezeit ~{wait} min")
    meta = html.escape(" · ".join(meta_bits))
    return f"""
    <figure class="cam{relevant}">
      <img src="{img}" alt="HAK Kamera {name}" loading="lazy" />
      <figcaption class="cam-body">
        {sev_html}
        <p class="cam-name">{name}</p>
        <p class="cam-meta">{meta}</p>
      </figcaption>
    </figure>
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
    # Efficient single fetch
    config = load_config(config_path)
    alerts = fetch_all(config)
    payload = _payload_from_alerts(config, alerts)
    html_path = out / "index.html"
    html_path.write_text(render_html(payload), encoding="utf-8")
    (out / "status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]Dashboard geschrieben:[/green] {html_path}")
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
