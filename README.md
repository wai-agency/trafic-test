# Stuttgart → Bužim Traffic Monitor

Lokales Stau-/Grenz-Überwachungssystem für die Fahrt **Stuttgart → Bužim** (über Karawanken + Maljevac), plus konkrete Reiseempfehlung.

## Reise-Empfehlung (Stand: Dienstag-Fahrt)

Morgen ist **Dienstag** — unter der Woche einer der besten Tage.

### Beste Abfahrt
- **Ideal: 03:00–05:00** ab Stuttgart (Europe/Berlin)
- Alternative: **heute Abend ab ~21:00** (Nachtfahrt), wenn du fit bist — Karawanken dann oft am frühen Morgen freier
- Meiden: Freitag 14–20, Samstag 06–15

### Beste Route
**Stuttgart → A8 → München → Salzburg → A10 Tauern → Villach → A11 Karawanken → SI-A2 → Ljubljana → Zagreb → Karlovac/Vojnić → Maljevac → Velika Kladuša → Bužim**

- OSRM freier Verkehr: ca. **930 km / ~11 h**
- Realistisch im Sommer: **12–15 h** (+ Stau/Grenze)

**Grenze:** Für Bužim meist **Maljevac / Velika Kladuša**. Wenn dort lange Kolonnen: **Izačić** (Bihać) als Alternative prüfen.

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

### Dauerhaft überwachen

```bash
traffic-monitor watch --interval 300
# oder
docker compose up -d --build
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
| GPMaljevac | nein | News/Schlagzeilen Grenze & Korridor |
| Nakordoni | optional Key | Live Wartezeiten HR↔BA |

\* In manchen Netzen ist `promet.si` blockiert; der Monitor läuft trotzdem mit den anderen Quellen weiter.
