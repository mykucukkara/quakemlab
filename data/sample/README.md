# Sample dataset

A small, ready-to-run example so the notebooks (M1–M6) can be exercised end to
end without the full archive. **Five high-quality Mw ≥ 5 events** from the
national archive, each located by HypoInverse with low RMS and a small offset to
the AFAD catalogue:

| Event | Origin (UTC) | Mw | Stations (sample / full) | RMS (s) | Offset to AFAD |
|---|---|---|---|---|---|
| 540341 | 2023-01-07 01:52:57 | 5.0 | 15 / 128 | 0.12 | 0.6 km |
| 543917 | 2023-02-07 03:13:12 | 5.4 | 15 / 70 | 0.07 | 1.7 km |
| 543434 | 2023-02-06 02:01:44 | 5.3 | 15 / 73 | 0.02 | 4.0 km |
| 515594 | 2021-11-08 17:43:21 | 5.1 | 15 / 111 | 0.18 | 1.5 km |
| 687395 | 2025-10-27 19:48:29 | 6.1 | 15 / 188 | 0.26 | 1.0 km |

## Layout

```
data/sample/
├─ bulletin.out            # AFAD Nordic (SEISAN) S-file, the 5 events (UTC)
└─ events/
   └─ {EventID}_{YYYYMMDD}_{HHMMSS}_{MagType}_{Mag}/
      └─ *.mseed           # three-component MiniSEED (UTC)
```

> **Station subset:** to keep the sample small (~19 MB total), each event's
> MiniSEED is trimmed to **15 stations**, preferring the analyst-picked stations
> in the bulletin so that the M5 pick evaluation stays meaningful. The full
> archives (hundreds of stations per event) are **not** redistributed — obtain
> them from the AFAD waveform data service (https://tdvms.afad.gov.tr) and the
> AFAD bulletins (https://deprem.afad.gov.tr).

> All times — in folder names, in the bulletin and in the MiniSEED — are **UTC**.

Point the M1–M3 notebooks at this folder (`bulletin.out` and `events/`) to run
the workflow on the sample.
