#!/usr/bin/env bash
# Worker'ı Cloud Run'a deploy eder. Build context = REPO KÖKÜ (bu betik kökten çağrılır).
# Kullanım: BOLGE=ege ./deploy/deploy_worker.sh
set -euo pipefail
BOLGE="${BOLGE:-ege}"
REGION="${REGION:-europe-west3}"
SERVICE="${SERVICE:-quakemlab-worker-$BOLGE}"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --set-env-vars "BOLGE=$BOLGE" \
  --cpu 1 --memory 2Gi --min-instances 1 --max-instances 1 \
  --no-cpu-throttling --timeout 300
# NOT: gcloud --source tek Dockerfile bekler. Bu repo çok servisli olduğundan
# ya kökte bir Dockerfile bulundurun ya da `gcloud builds submit`/Docker ile
# `-f services/worker/Dockerfile .` kullanın. Sırlar (TELEGRAM_*, GCS_BUCKET_NAME)
# --set-env-vars yerine Secret Manager ile verilmelidir.
