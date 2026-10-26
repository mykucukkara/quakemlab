"""Cloud Run sarmalayıcısı: live_worker'ı arka planda 7/24 çalıştırır ve Cloud Run'ın
beklediği HTTP sağlık endpoint'ini ($PORT) sunar.

live_worker.py bir SeedLink dinleyicisidir (istek-güdümlü değil); bu yüzden Cloud Run'da
--min-instances=1 --no-cpu-throttling ile 'her zaman açık' servis olarak koşar.
"""
import os
import threading

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="Sismik Canlı Dedektör (live_worker)")
_started = threading.Event()


@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "worker_started": _started.is_set()}


@app.on_event("startup")
def _launch_worker():
    if _started.is_set():
        return
    _started.set()

    def _run():
        # AĞIR import (torch/seisbench/pyocto + hız modeli) ARKA PLANDA yapılır ki
        # uvicorn hemen $PORT'u dinlesin (Cloud Run health check'i zaman aşımına uğramasın).
        from quakemlab_core import engine
        engine.main()

    threading.Thread(target=_run, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
