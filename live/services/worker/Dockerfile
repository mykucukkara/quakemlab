# QuakeMLab canlı tespit WORKER'ı.
# ÖNEMLİ: build context = REPO KÖKÜ (pyproject.toml, src/, regions/, data/ erişimi için):
#   gcloud run deploy sismik-worker --source . -f services/worker/Dockerfile ...
#   (veya docker build -f services/worker/Dockerfile .)
FROM python:3.10-slim

WORKDIR /app

# pyocto/obspy derlemesi + sourcespec'in git'ten kurulumu için.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git \
    && rm -rf /var/lib/apt/lists/*

# pip'i önce güncelle (eski pip PyTorch indeks adlarını normalize edemeyip derlemeye düşüyor).
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# CPU-only PyTorch (PhaseNet/seisbench için; CUDA bloat'ından kaçın). Sürüm sabit.
RUN pip install --no-cache-dir torch==2.14.0+cpu \
    --extra-index-url https://download.pytorch.org/whl/cpu

# Paket + bölge yapılandırmaları + istasyon yanıtları.
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY regions/ ./regions/
COPY data/ ./data/
RUN pip install --no-cache-dir ".[service]"

# Worker servis sarmalayıcısı (paket değil; doğrudan çalıştırılır).
COPY services/worker/worker_service.py ./

ENV PORT=8080
ENV PYTHONUNBUFFERED=1
# BOLGE=ege varsayılan; regions/ege.yaml repoda. Başka bölge: BOLGE=<ad> +
# QUAKEMLAB_REGION_FILE veya regions/<ad>.yaml (deploy sırasında eklenir).
EXPOSE 8080

CMD ["python", "worker_service.py"]
