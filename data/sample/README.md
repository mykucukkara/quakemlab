# Sample dataset

A small example so the notebooks (M1–M6) can be run end to end without the full
archive.

Expected contents (to be added before the public release):

```
data/sample/
├─ bulletin.out            # a short AFAD Nordic (SEISAN) S-file, a few events
└─ events/
   └─ {EventID}_{YYYYMMDD}_{HHMMSS}_{MagType}_{Mag}/
      └─ *.mseed           # three-component MiniSEED for each event (UTC)
```

Recommended: ~5 well-recorded events from the Marmara archive (e.g. the M ≥ 4
events), keeping the folder under a few tens of MB.

> All times — in folder names and in the MiniSEED — must be **UTC**.

The full waveform archives are **not** redistributed here; obtain them from the
AFAD waveform data service (https://tdvms.afad.gov.tr) and the AFAD bulletins
(https://deprem.afad.gov.tr).
