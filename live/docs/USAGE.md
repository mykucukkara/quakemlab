# QuakeMLab — Usage & Reference

Detailed guide for running, configuring, and extending the pipeline, plus a
module/function reference. For the high-level overview see the [README](../README.md).

---

## 1. Architecture & data flow

```
                 ┌────────────────────────── quakemlab_core.engine ──────────────────────────┐
 SeedLink v3  ─▶ │ acquisition ─▶ ingest ─▶ PhaseNet pick ─▶ pool ─▶ PyOcto associate ─▶     │
 (per server,    │  (run_server_group)      (_ingest_stream) (maybe_analyze/analyze_streams)  │
  multi-station) │                                                    │                        │
 FDSN backup  ─▶ │  (run_fdsn_only)                                   ▼                        │
                 │                                       HypoInverse location (HTTP API)        │
                 │                                                    │                        │
                 │                        Wood-Anderson ML  ◀─────────┤                        │
                 │                        SourceSpec Mw (bg job) ◀─────┘                        │
                 │                                                    ▼                        │
                 │                       catalog + picks + mseed  ──▶ GCS   ──▶ Telegram / X    │
                 └───────────────────────────────────────────────────────────────────────────┘
                                                              ▲
   services/api (HypoInverse hyp1.40) ────────────────────────┘
   services/viewer (Streamlit) reads the GCS catalog for display
```

Key ideas:

- **Acquisition** groups stations by SeedLink server and opens **one connection per
  server** (multi-station). Stations without public SeedLink fall back to periodic
  FDSN `dataselect` polling (~5 min delayed).
- **Picking** runs PhaseNet on a rolling 15-minute buffer; picks above threshold go
  into a shared pool.
- **Association** (PyOcto) runs in a UTM km-Cartesian plane; the search box is
  derived from the station envelope + `pad_km`.
- **Location** is delegated to the HypoInverse API (`services/api`) over HTTP.
- **Magnitude**: local Wood-Anderson **ML** for every event; **Mw** (SourceSpec) as a
  background job for events with `ML ≥ mw_min_ml`.
- **Persistence**: catalog CSV, picks CSV, per-station mseed, and a STATUS.json
  health file are written to GCS under the region's `events_dir`.
- **Refinement**: an event may be relocated up to `REFINE_MAX_COUNT` times as delayed
  stations arrive; a worse solution is rejected (the previous one is kept).

---

## 2. Installation

```bash
python -m venv .venv && source .venv/bin/activate   # Python >= 3.10
pip install -e ".[service]"
```

Notes:
- `seisbench` pulls **PyTorch** (install the CPU wheel for servers, see the worker
  Dockerfile: `torch==2.14.0+cpu`).
- `sourcespec` (spectral Mw) is installed from git because the PyPI release conflicts
  with `matplotlib>=3.9`. If SourceSpec is unavailable the system still runs, only
  without Mw.
- `[service]` adds `fastapi`+`uvicorn` for the Cloud Run worker wrapper.

---

## 3. Running

### Worker (detector)

```bash
export BOLGE=ege
export GCS_BUCKET_NAME=your-bucket           # catalog/data store
export PYOCTO_API_URL=https://<your-hypoinverse-api>
python services/worker/worker_service.py     # serves /health on $PORT, runs detector in a thread
```

### Location API (HypoInverse)

```bash
cd services/api && uvicorn pyocto_api:app --host 0.0.0.0 --port 8080
```
Requires the `hyp1.40` binary on `PATH` (see README limitations).

### Viewer (Streamlit)

```bash
cd services/viewer && streamlit run app.py
```

---

## 4. Region YAML schema (`regions/<region>.yaml`)

| Field | Type | Meaning |
|---|---|---|
| `name` | str | Region id (matches `BOLGE`) |
| `display_name.tr` / `.en` | str | Human-readable names |
| `events_dir` | str | GCS prefix for this region's catalog/data |
| `utm_zone` | int | UTM zone for the km-Cartesian projection |
| `pad_km` | float | Margin added around the station envelope for the search box |
| `velocity_model.depth/vp/vs` | list | 1-D layered model (km, km/s) |
| `zlim` | [min,max] | Depth search range (km) |
| `model_xdist` / `model_zdist` | float | Velocity-model table extents (km) |
| `stations[]` | list | `{net, sta, sl:[host,port]|null, fdsn, fdsn_window?}` |
| `station_coords{}` | map | `STA: {x: lon, y: lat, z?}` |
| `response.source` | `fdsn`\|`local` | Where instrument responses come from |
| `response.fdsn_groups[]` | list | `{provider, net, stations}` (source=fdsn) |
| `thresholds{}` | map | Detection/quality thresholds (see below) |

**Thresholds** (env var overrides in parentheses): `pick_threshold`
(`PICK_THRESHOLD`), `s_pick_threshold` (`S_PICK_THRESHOLD`), `min_p_stations`
(`MIN_P_STATIONS`), `min_s_picks` (`MIN_S_PICKS`), `snr_min` (`SNR_MIN`),
`min_clean_p_stations` (`MIN_CLEAN_P_STATIONS`), `mw_min_ml` (`MW_MIN_ML`),
`delay_offset_sec` (`DELAY_OFFSET_SEC`).

A station with `sl: null` is **FDSN-only** (delayed); one with `sl: [host, port]` is
real-time SeedLink with `fdsn` as its backup provider. `fdsn_window` widens the first
delayed fetch.

---

## 5. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BOLGE` | `ege` | Region to load |
| `QUAKEMLAB_REGION_FILE` | — | Explicit region-YAML path |
| `QUAKEMLAB_REGIONS_DIR` | — | Directory holding `<region>.yaml` |
| `QUAKEMLAB_STATION_XML_DIR` | `data/station_xml/<BOLGE>` | Local responses (source=local) |
| `GCS_BUCKET_NAME` | project bucket | Catalog/data store |
| `PYOCTO_API_URL` | project API | HypoInverse location service |
| `WEB_APP_URL` | localhost | Base URL used in notification deep-links |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Telegram notifications |
| `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | — | X (Twitter) |
| `X_TWEET_LINK` | `0` | Append the event link to tweets when `1` |
| threshold vars | from YAML | Override region thresholds |

---

## 6. Module & function reference

### `quakemlab_core.config`
Loads the region YAML (selected by `BOLGE`) and builds all derived objects/constants.
`engine.py` imports these via `from .config import *`.

- **`_find_region_file(bolge)`** → `Path`. Locates `<bolge>.yaml` via
  `QUAKEMLAB_REGION_FILE`, then `QUAKEMLAB_REGIONS_DIR`, then repo/CWD/`/app` `regions/`.
- **`to_lonlat(x_km, y_km)`** → `(lon, lat)`. Inverse UTM projection.
- **`ensure_catalog_files()`**. Creates the local PICKS/CATALOG CSVs (with header
  migration) at worker start-up — called from `engine.main()`, not at import.
- Exposes: `STATIONS`, `station_df`, `station_df_km`, `_STA_LATLON`, `proj`,
  `XLIM`/`YLIM`/`ZLIM`, `model_df`, `velocity_model`, `associator`, `EVENTS_DIR`,
  `BOLGE`/`BOLGE_ADI`, all SeedLink/FDSN/heartbeat constants, `FDSN_BASE`, `PAZ_WA`,
  `RESPONSE_SOURCE`/`FDSN_RESPONSE_GROUPS`/`STATION_XML_DIR`, and every threshold.

### `quakemlab_core.engine`
The real-time detection engine. Entry point: `main()`.

- **`main()`**. Loads PhaseNet, seeds the catalog from GCS, starts the response-inventory
  fetch, groups stations by server, and launches acquisition + heartbeat threads.
- **`run_server_group(cfgs)`**. One SeedLink connection for all stations on a
  `(host, port)`; reconnects on failure and falls its stations back to FDSN after
  `SL_MAX_FAILS`.
- **`run_fdsn_only(cfg)`**. Periodic FDSN `dataselect` polling for a delayed station.
- **`_sl_negotiate(sock, cfgs)`** / **`_sl_stream_once(cfgs)`**. Raw multi-station
  SeedLink v3 negotiation (HELLO/STATION/SELECT/DATA/END, no TIME) and framing loop.
- **`_http_waveforms(prov, net, sta, t_start, t_end)`** / **`_fdsn_fetch_once(cfg, client)`**.
  Direct-HTTP FDSN dataselect fetch (avoids ObsPy service discovery).
- **`_ingest_stream(key, incoming)`**. Merges an incoming stream into the rolling buffer.
- **`maybe_analyze()`** / **`analyze_streams()`**. Debounced trigger and the core
  detection routine: PhaseNet picking, SNR gate, PyOcto association, location via the
  API, ML, catalog write, refinement, and background Mw scheduling.
- **`_pick_snr(sta, pick_time, phase, p_time)`** → float. Signal-to-noise ratio around a
  pick (noise window taken before that station's P); used by the SNR gate.
- **`fetch_response_inventory()`**. Builds the instrument-response inventory per the
  region's `response` config (FDSN groups or local StationXML); sets `station_inventory`.
- **`_http_inventory(prov, net, stas)`**. Direct-HTTP FDSN station (level=response) query.
- **`_wa_amp_mm(stream_slice)`** → mm. Peak Wood-Anderson amplitude on the horizontals.
- **`_median_ml(wa_amps, ev_lat, ev_lon, ev_depth)`** → ML (Hutton-Boore 1987).
- **`get_location_from_api(assigned_df, trial=None)`**. Posts picks/stations to the
  HypoInverse API and returns the located solution.
- **`_best_band(stream)`** / **`_station_inventory_file()`** /
  **`_spectral_mw_job(event_id, lat, lon, depth, orig_t_abs, mseed_files, max_dist, cat_row)`**.
  SourceSpec Mw background job and its helpers (writes the merged StationXML, runs
  SourceSpec, and patches the catalog row's Mw fields).
- **`upload_and_notify(event_id, lat, lon, depth, orig_t_abs, loc_name, sta_names, mseed_files, mag=None, rms=None, n_sta=None, notify=True)`**.
  Uploads catalog/picks (no-cache) + mseed + zip to GCS and sends the Telegram message
  (region-labelled) and an X post; gated to `n_sta ≥ 3`.
- **`_replace_catalog_row(event_id, new_row)`** / **`_replace_pick_rows(event_id, pick_rows)`**.
  In-place update of an event during refinement.
- **`heartbeat()`** / **`_publish_status(now, alive, total, sta_status, npool)`**.
  Periodic freshness logging and STATUS.json publishing (makes silent blindness visible).
- **`_seed_catalog_from_gcs()`**. On restart, seeds the local catalog/picks from GCS so
  history is preserved.

### `quakemlab_core.geocode` (pure, no global state)
- **`hav_km(la1, lo1, la2, lo2)`** → km. Great-circle distance.
- **`azimuthal_gap(ev_lat, ev_lon, sta_names)`** → degrees | None. Largest station
  azimuth gap around the epicenter (used when HypoInverse can't provide gap).
- **`reverse_geocode(lat, lon)`** → str. Place name via Nominatim; `"Bilinmiyor"` if unknown.

### `quakemlab_core.spectral_mw`
- **`compute_spectral_mw(event_id, lat, lon, depth_km, origin_utc, mseed_files, inventory_path, max_dist_km=300.0, workroot=None, timeout=600)`**
  → dict | None. Runs SourceSpec and returns `{mw, mw_err, mw_n, fc, m0, ...}`; never
  raises (returns None on failure).

### `quakemlab_core.notify`
- **`post_to_x(text)`**. Posts a tweet via X API v2 (OAuth 1.0a); silently skipped if
  keys are missing. Telegram sending lives in `engine.upload_and_notify`.

### `services/worker/worker_service.py`
FastAPI wrapper: serves `/` and `/health` on `$PORT` and launches `engine.main()` in a
background thread so Cloud Run's health check responds immediately.

---

## 7. Data model

**Catalog CSV** (`<events_dir>/LIVE_CATALOG.csv`) columns:
`event_id, lat, lon, depth, origin_time, loc_name, mag, rms, n_sta, erh, erz, gap,
n_p, n_s, proc_s, model, delayed, proc_net_s, mw, mw_err, mw_n, fc`.

**GCS layout** per region:
```
<events_dir>/LIVE_CATALOG.csv
<events_dir>/LIVE_PICKS.csv
<events_dir>/STATUS.json
<events_dir>/mseed/<event_id>_<STA>.mseed
<events_dir>/zip/<event_id>.zip
```

Hot objects are uploaded with `Cache-Control: no-cache` and read generation-pinned,
because the public bucket otherwise serves stale copies for up to an hour.

---

## 8. Deployment (Cloud Run)

Example scripts are in `deploy/`. The worker must build from the **repo root** (it
needs `pyproject.toml`, `src/`, `regions/`, `data/`); the API and viewer build from
their own `services/<name>/` directories. Supply secrets (`TELEGRAM_*`,
`GCS_BUCKET_NAME`) via Secret Manager, not `--set-env-vars`. Run the worker with
`--min-instances 1 --no-cpu-throttling` (it is an always-on listener, not
request-driven).

---

## 9. Extending

- **New region:** copy `regions/ege.yaml`, adjust, run with `BOLGE=<name>`.
- **Network without open FDSN responses:** put StationXML in
  `data/station_xml/<region>/` and set `response.source: local`.
- **New notification channel:** add a function in `notify.py` and call it from
  `engine.upload_and_notify`.
