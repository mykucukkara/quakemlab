#!/usr/bin/env bash
# HypoInverse lokasyon API'si. build context = services/api/
# UYARI: services/api/Dockerfile private bir base imajından türer (hyp1.40 binary'si
# orada gömülü — HypoInverse modern gfortran ile temiz derlenmiyor). Public kullanım
# için hyp1.40'ı vendor'layıp derleyen bir Dockerfile gerekir (bkz. README).
set -euo pipefail
REGION="${REGION:-europe-west3}"
gcloud run deploy quakemlab-api --source services/api --region "$REGION" \
  --cpu 2 --memory 2Gi --timeout 900
