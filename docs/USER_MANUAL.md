# QuakeMLab — User Manual & Tutorial

This manual walks through the offline workflow (notebooks **M1–M6**) and points to
the real-time engine. It is written so that a reviewer or a new network can
reproduce the paper's pick-level and hypocentre-level results.

## 1. Installation

```bash
python -m venv .venv && source .venv/bin/activate      # Python 3.10–3.13
pip install obspy seisbench torch pandas numpy scipy matplotlib pyocto jupyterlab
```

A CPU build of PyTorch is sufficient; no GPU is required (all results in the
paper were produced on CPU).

## 2. Input data layout

The workflow expects, per event, a folder named
`{EventID}_{YYYYMMDD}_{HHMMSS}_{MagType}_{Mag}` (e.g.
`293183_20150328_013454_Mw_50`) containing the event's MiniSEED files, plus the
AFAD Nordic (SEISAN) bulletin S-file for the ground-truth picks.

> **Critical:** every time encoded in a folder name **and** every waveform window
> must be in **UTC**. A time base that silently interprets UTC origin times as
> local time shifts every window by three hours and makes the pick evaluation
> return zero true positives. M5 writes a per-event table of automatic vs. manual
> times precisely so that this is caught immediately (see paper §5.4).

A small ready-to-run example is in [`../data/sample/`](../data/sample/).

## 3. Running the workflow (in order)

| Step | Notebook | What it does |
|---|---|---|
| M1 | `notebooks/M1_parse_bulletins.ipynb` | Parse the Nordic S-file, match each bulletin event to its waveform folder, write `sfile_parsed.json`. |
| M2 | `notebooks/M2_manual_pick_table.ipynb` | Flatten `sfile_parsed.json` into a manual-pick table keyed by (event_id, station, component, phase). |
| M3 | `notebooks/M3_preprocess_waveforms.ipynb` | Merge, demean/detrend, 1–45 Hz band-pass, resample to 100 Hz; store `.npy` traces + an index CSV. |
| M4 | `notebooks/M4_multimodel_picking.ipynb` | Run the 8 SeisBench architectures × their registry checkpoints (64 configs); write one pick table per config. |
| M5 | `notebooks/M5_pick_evaluation.ipynb` | Join automatic vs. manual picks on the composite key, keep the highest-confidence pick per key, score Precision/Recall/F1/MAE/RMSE per phase with tolerance τ = 1.0 s; produce the comparison figures. |
| M6 | `notebooks/M6_data_quality.ipynb` | Coverage and data-quality diagnostics. |

Each stage writes plain tabular/JSON files with fixed schemas, so a stage can be
re-run or inspected independently. Long runs are resumable: existing
per-configuration outputs and per-trace arrays are skipped.

## 4. Association, location and magnitude (M7)

Phase association (PyOcto, UTM-36), location (HypoInverse 1.40 from HYPO71-format
phase files, multi-start trial depths, RMS + √(ERH²+ERZ²) selection) and
Wood–Anderson local magnitude are implemented in
[`../live/src/quakemlab_core/engine.py`](../live/src/quakemlab_core/engine.py)
and the location service
[`../live/services/api/pyocto_api.py`](../live/services/api/pyocto_api.py).

HypoInverse's legacy Fortran (`hyp1.40`) is **not** redistributed; compile it
with `gfortran -std=legacy -ffixed-line-length-none`.

## 5. Real-time engine

```bash
pip install -e "live/.[service]"
export BOLGE=ege
python live/services/worker/worker_service.py
```

Everything region-specific (stations, coordinates, velocity model, search box,
thresholds) lives in `live/regions/<region>.yaml`; adding a region is a new YAML
plus `BOLGE=<name>`, no code changes. See
[`../live/docs/USAGE.md`](../live/docs/USAGE.md).

## 6. Reproducing the paper figures

- **Pick-level (Marmara):** M5 produces the precision–recall and F1–MAE figures
  and the per-architecture table.
- **Hypocentre-level (national Mw ≥ 5):** run M1–M4 on the national archive, then
  the location service; compare against AFAD/EMSC/NOA via M8.
