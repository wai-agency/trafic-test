# Bužim → Waiblingen Traffic Monitor (Rückfahrt)

Lokales Stau-/Grenz-Überwachungssystem für die **Rückfahrt Bužim → Waiblingen** (über Maljevac + Karawanken), plus konkrete Reiseempfehlung.

**Status:** Monitor ist **aktiv** (Rückfahrt) — Live-Refresh ~alle 20 Min.

## Reise-Empfehlung (Rückfahrt)

### Beste Abfahrt
- **Ideal: 03:00–05:00** ab Bužim (Europe/Berlin)
- Alternative: **Abend ab ~21:00** (Nachtfahrt), wenn du fit bist
- Meiden: Freitag 14–20, Samstag 06–15

### Beste Route
**Bužim → Maljevac → Lučko/Zagreb → SI-A2 → A11 Karawanken → Villach → A10 Tauern → Salzburg → A8 → Waiblingen**

- OSRM freier Verkehr: ca. **930 km / ~11 h**
- Realistisch im Sommer: **12–15 h** (+ Stau/Grenze)

**Grenze:** Ausreise meist **Maljevac (BiH→HR)**. Wenn dort lange Kolonnen: **Izačić** (Bihać) als Alternative prüfen.

### Was du beachten solltest
| Land | Pflicht / Kosten |
| --- | --- |
| Österreich | Vignette (ASFINAG) + Sondermaut **A10 Tauern** + **Karawanken A11** |
| Slowenien | E-Vignette vorab: [evinjeta.dars.si](https://evinjeta.dars.si) |
| Kroatien | Streckenmaut (Ticket/Karte/ENC) |
| BiH | Dokumente, Versicherung/grüne Karte prüfen |

Zusätzlich: Kühlwasser/Reifen/Klima checken, Wasser mitnehmen, Pause alle 2–3 h, Vignetten **vor** der Autobahn kaufen.

### Nützliche Live-Tools
- Deutschland: [autobahn.de API](https://verkehr.autobahn.de/o/autobahn/) / Google Maps / Waze
- Österreich: [ASFINAG App](https://www.asfinag.at/) + RSS Verkehrsmeldungen
- Slowenien: [promet.si](https://www.promet.si/) / DarsPromet+
- Kroatien: [HAK](https://www.hak.hr/info/stanje-na-cestama)
- Grenze BiH: [GPMaljevac](https://gpmaljevac.com/), [Nakordoni HR↔BA](https://nakordoni.eu/en/stat/16/17/4)

---

## Monitor installieren

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

### Sofort Empfehlung + Live-Check

```bash
traffic-monitor recommend
traffic-monitor check
```

### Telegram-Alerts

1. Bei [@BotFather](https://t.me/BotFather) Bot erstellen → Token in `.env` als `TELEGRAM_BOT_TOKEN`
2. Bot anschreiben, Chat-ID ermitteln, als `TELEGRAM_CHAT_ID` setzen
3. Testen:

```bash
set -a && source .env && set +a
traffic-monitor check --notify
```

### Dauerhaft überwachen (PC)

```bash
traffic-monitor watch --interval 300
# oder
docker compose up -d --build
```

### In der Cloud ohne PC (GitHub Actions + mobiles Dashboard)

Der Workflow prüft ca. **alle 20 Minuten**, schickt Telegram-Alerts und deployed ein **mobil-optimiertes Dashboard** auf GitHub Pages. Zuverlässigkeit: GitHub-Cron allein ist unzuverlässig — deshalb startet *Traffic Monitor Requeue* nach jedem erfolgreichen Lauf automatisch den nächsten (~19 Min Pause). Loop stoppen: Repo-Variable `MONITOR_KEEPALIVE=false`. Manuell: Actions → *Traffic Monitor* → *Run workflow*. Das Dashboard lädt Kamerabilder **live (~alle 10s)**: HAK (Maljevac/Lučko, HD ~1280×720) plus **Karawanken-Tunnel** (ASFINAG A11 + DARS Hrušica). OpenAI Vision (gpt-5.6-terra) zählt nur HAK-Kameras, wenn der Workflow läuft (Cache ~20 Min).

1. Secrets (erledigt, wenn gesetzt):  
   https://github.com/wai-agency/trafic-test/settings/secrets/actions  
   - `TELEGRAM_BOT_TOKEN`  
   - `TELEGRAM_CHAT_ID` = `5024687751`
2. Pages Source: **GitHub Actions**  
   https://github.com/wai-agency/trafic-test/settings/pages
3. Workflow läuft bei Push auf `main`, alle 20 Min, oder manuell

Dashboard: **https://wai-agency.github.io/trafic-test/**

### Live-Alternativrouten per Telegram
Bei Stau berechnet der Monitor **im Moment** mehrere Kandidaten und schickt die **aktuell schnellste**
mit Google-Maps-Link:

1. Fahrzeit live (wenn Key vorhanden):
   - `GOOGLE_MAPS_API_KEY` (Google Routes/Directions mit Verkehr) **oder**
   - `TOMTOM_API_KEY` (TomTom traffic)
2. Sonst: OSRM-Fahrzeit **plus Live-Grenzwartezeit** von Nakordoni
3. Vergleich z. B. Karawanken+Maljevac vs Graz/Maribor vs Izačić → beste Option + Vergleichstabelle

Secrets: https://github.com/wai-agency/trafic-test/settings/secrets/actions

### Dashboard lokal

```bash
traffic-monitor dashboard --out site
traffic-monitor dashboard --serve --port 8080
# Handy im gleichen WLAN: http://<deine-pc-ip>:8080
```

### Optional: Grenz-Wartezeiten API

Kostenlosen/Developer-Key von [Nakordoni](https://nakordoni.eu/en/developers/docs/border) in `NAKORDONI_API_KEY` legen. Dann warnt der Monitor bei langen Schlangen an Maljevac/Izačić.

Watchpoints/Keywords: `traffic_monitor/config/watchpoints.yaml`

## CLI

```text
traffic-monitor recommend
traffic-monitor check
traffic-monitor check --notify
traffic-monitor watch --interval 300 --console-only
```

## Quellen im Monitor

| Quelle | Auth | Inhalt |
| --- | --- | --- |
| autobahn.de | nein | Stau/Sperrungen A8/A7/… Korridor DE |
| ASFINAG RSS | nein | AT-Meldungen (Karawanken/Tauern Keywords) |
| promet.si JSON | nein* | SI Events (wenn Endpoint erreichbar) |
| GPMaljevac | nein | RSS + Portal (Grenze/Korridor; gleiche Community wie FB) |
| Facebook GPMaljevac | optional `FACEBOOK_PAGE_ACCESS_TOKEN` | Direkte Page-Posts via Graph API |
| Nakordoni | optional Key | Live Wartezeiten HR↔BA |

\* In manchen Netzen ist `promet.si` blockiert; der Monitor läuft trotzdem mit den anderen Quellen weiter.
