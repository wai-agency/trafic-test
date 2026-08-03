# Manual Maljevac camera counts (ground truth notes)

Counted by hand from live HAK JPEGs on 2026-08-03 ~13:38 local.

## Cam 430 — Ausreise HR → BiH (`to_bih`)

Frame: `GP Maljevac, 03.08.2026. 13:38:32`

| Zone | Manual count | Notes |
|------|-------------:|-------|
| BiH entry queue (center lane toward booths) | **20–25** | Dense single file; count car-by-car front→back |
| HR opposite lane (toward camera) | **0–1** | Do not count for `to_bih` |
| Parking right | **~10** | Ignore |

Dashboard with `gpt-5-nano` at 13:17 reported **6** for Einfahrt BiH — severe undercount.

## Cam 429 — Einreise BiH → HR (`to_hr`)

Frame: `GP Maljevac, 03.08.2026. 13:38:34`

| Zone | Manual count | Notes |
|------|-------------:|-------|
| HR toward camera (`▼ HR ▼`) | **2–5** | Only this lane for `to_hr` |
| BiH away (`▲ BiH ▲`) | **8–10** | Do not count for `to_hr` |
| Parking L/R | **~5** | Ignore |

## Expected dashboard mapping

- Einfahrt BiH ← cam 430 BiH queue ≈ **20–25**
- Einfahrt HR ← cam 429 HR lane ≈ **2–5**
