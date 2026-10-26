# QuakeMLab

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23668783.svg)](https://doi.org/10.5281/zenodo.23668783)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**A model-agnostic deep-learning workflow for automated seismic phase picking,
earthquake location and catalogue verification in Türkiye.**

QuakeMLab converts raw three-component waveforms into automatically located,
catalogue-checked earthquake solutions using publicly available pretrained
deep-learning models. It (1) builds ground-truth pick tables from Nordic-format
analyst bulletins, (2) runs 64 architecture–checkpoint combinations of eight
[SeisBench](https://github.com/seisbench/seisbench) pickers through a common
pre-processing chain, (3) scores every configuration against manual picks,
(4) forms four pick sets per architecture for association, location and
local-magnitude estimation, and (5) checks each solution against reference
catalogues. A region-parametric engine runs the same pipeline in real time.

> This repository accompanies the QuakeMLab software report (Tezel et al.,
> submitted) and is the reference implementation that reproduces its results.
> Sakarya University — TÜBİTAK–AFAD 1001-UDAP project **124E294**.

## Repository layout

```
notebooks/      The offline workflow (modules M1–M6), one notebook per stage
  M1_parse_bulletins.ipynb      Parse Nordic (SEISAN) S-files → events + picks
  M2_manual_pick_table.ipynb    Export the flat ground-truth manual-pick table
  M3_preprocess_waveforms.ipynb Merge, detrend, 1–45 Hz band-pass, resample 100 Hz
  M4_multimodel_picking.ipynb   Run 64 SeisBench architecture–checkpoint configs
  M5_pick_evaluation.ipynb      Precision / recall / F1 / MAE vs. manual picks (τ=1 s)
  M6_data_quality.ipynb         Data-quality and coverage diagnostics
live/           Region-parametric real-time engine (M7–M9 in production)
  src/quakemlab_core/           acquisition → picking → association → location →
                                magnitude → catalogue → notification
  services/{worker,api,viewer}  Cloud Run wrappers (SeedLink worker, HypoInverse
                                location API, Streamlit viewer)
  regions/ege.yaml              Example region definition (public data sources)
  data/station_xml/             Bundled instrument responses (networks w/o open FDSN)
  docs/USAGE.md                 Region-YAML schema, environment variables, reference
data/sample/    A small example dataset so the notebooks can be run end-to-end
docs/           User manual and tutorial
```

### How the notebooks map to the paper modules

| Paper module | Notebook / code | Output |
|---|---|---|
| M1 Bulletin parser + event matcher | `notebooks/M1_parse_bulletins.ipynb` | `sfile_parsed.json` |
| M2 Ground-truth export | `notebooks/M2_manual_pick_table.ipynb` | manual-pick table |
| M3 Waveform pre-processing | `notebooks/M3_preprocess_waveforms.ipynb` | `.npy` traces + index |
| M4 Multi-model picking | `notebooks/M4_multimodel_picking.ipynb` | one pick table per config |
| M5 Pick evaluation | `notebooks/M5_pick_evaluation.ipynb` | P/R/F1/MAE tables, figures |
| M6 Pick-set selection | `live/src/quakemlab_core/engine.py` (`pickset_*`) | four pick sets |
| M7 Association · location · ML | `live/src/quakemlab_core/engine.py`, `live/services/api/pyocto_api.py` | PyOcto + HypoInverse 1.40 + Wood–Anderson ML |
| M8 Catalogue verification | `live/services/api/pyocto_api.py` | nearest reference solution + offset |
| M9 Dissemination | `live/services/viewer/app.py` (Streamlit); public portal (ASP.NET) | maps, tables, portal |

## Quick start (offline workflow)

```bash
python -m venv .venv && source .venv/bin/activate   # or conda
pip install obspy seisbench torch pandas numpy scipy matplotlib pyocto jupyterlab
jupyter lab            # then run notebooks M1 → M6 in order
```

The notebooks read event waveforms from an `events/` directory (one folder per
event, named `{EventID}_{YYYYMMDD}_{HHMMSS}_{MagType}_{Mag}`, times in **UTC**)
and the AFAD Nordic bulletin. A small ready-to-run example lives in
`data/sample/`; see [docs/USER_MANUAL.md](docs/USER_MANUAL.md).

## Quick start (real-time engine)

```bash
pip install -e "live/.[service]"
export BOLGE=ege
python live/services/worker/worker_service.py
```

See [live/docs/USAGE.md](live/docs/USAGE.md) for the region-YAML schema,
environment variables and deployment.

## Data availability

- **Waveforms** — Turkish National Seismic Network (TU) and KOERI (KO), obtained
  from the AFAD waveform data service (https://tdvms.afad.gov.tr); real-time data
  via AFAD SeedLink. Only a small sample is bundled here; the full archives are
  not redistributed.
- **Bulletins / event parameters** — AFAD (https://deprem.afad.gov.tr).
- **Reference catalogues** — EMSC (https://www.seismicportal.eu) and NOA
  (https://eida.gein.noa.gr) FDSN event services.
- **Pretrained weights** — distributed through SeisBench.

## Known limitations

- **HypoInverse `hyp1.40`** is not redistributed here; its legacy Fortran source
  must be compiled separately (`-std=legacy -ffixed-line-length-none`). See
  `live/docs/USAGE.md`.
- Large waveform archives are **not** committed; use the AFAD service above.
- Notebook comments are partly in Turkish; the README, user manual and
  `live/docs/USAGE.md` are in English.

## Citation

If you use QuakeMLab, please cite:

> Tezel, T., Erden, C., Küçükkara, M. Y., Arıkan, H., Yanık, K., & Utkucu, M.
> (2026). *QuakeMLab: a model-agnostic deep-learning workflow for automated
> seismic phase picking, earthquake location and catalogue verification in
> Türkiye.* (software report, submitted).

See [`CITATION.cff`](CITATION.cff). Archived release:
[https://doi.org/10.5281/zenodo.23668783](https://doi.org/10.5281/zenodo.23668783).

## Open-source components

SeisBench (PhaseNet, EQTransformer, GPD, BasicPhaseAE, PhaseNetLight,
OBSTransformer, LFEDetect, Skynet), PyOcto, HypoInverse 1.40, ObsPy, pyproj,
geopy. Automatic solutions are publicly viewable at
https://quakemlab.sakarya.edu.tr (academic research platform — not an official
earthquake information or early-warning service).

## License

MIT — see [LICENSE](LICENSE).
