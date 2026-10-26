# Workflow notebooks (M1–M5)

Run these **in order**. Out of the box they run on the bundled 5-event sample in
[`../data/sample/`](../data/sample/); the first cell of each notebook
(`QuakeMLab sample configuration`) resolves the sample path automatically.

| Order | Notebook | Reads | Writes |
|---|---|---|---|
| 1 | `M1_parse_bulletins.ipynb` | `data/sample/bulletin.out`, `data/sample/events/` | `sfile_parsed.json` |
| 2 | `M2_manual_pick_table.ipynb` | `sfile_parsed.json` | `manual_picks.xlsx` |
| 3 | `M3_preprocess_waveforms.ipynb` | `sfile_parsed.json`, `events/` | `seisbench_ready/` (.npy + index) |
| 4 | `M4_multimodel_picking.ipynb` | `events/` (+ `seisbench_ready/`) | `Preds/` (one CSV per model) |
| 5 | `M5_pick_evaluation.ipynb` | `Preds/`, `manual_picks.xlsx` | `TUM_SONUCLAR_BIRLESIK_CLEAN.csv`, `Performance_Results*/` |

Intermediate outputs are written to the current working directory and are
git-ignored.

## Verified

The full chain M1 → M5 was executed on the bundled sample (5 Mw ≥ 5 events,
15 stations each): M1 matches all 5 events, M3 indexes 234 traces, M4 produces
picks, M5 merges and scores them. (Python 3.13, obspy 1.5, seisbench 0.11,
torch 2.x CPU.)

## Configuration (first cell of each notebook)

- `SAMPLE` / `SFILE_PATH` / `EVENTS_DIR` — point these at your own archive to run
  the full study instead of the sample (or set the `QUAKEMLAB_SAMPLE` env var).
- `DEMO_MODE = True` (M4) — runs **one lightweight model (PhaseNetLight)** so the
  sample finishes in seconds. Set `DEMO_MODE = False` to run all eight
  architectures × every SeisBench checkpoint (64 configurations; downloads many
  weights, takes much longer).
- `TAU_SECONDS = 1.0` (M5) — pick-matching tolerance. This matches the paper
  (§3.5). **Note:** the original notebook hard-coded 100 s; it is set to 1.0 s
  here for reproducibility — confirm the intended value before quoting metrics.

## Requirements

```bash
pip install obspy seisbench torch pandas numpy scipy matplotlib openpyxl tqdm jupyterlab
```

M4 downloads pretrained weights from SeisBench on first use (needs internet).
