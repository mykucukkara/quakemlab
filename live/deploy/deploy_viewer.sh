#!/usr/bin/env bash
# Streamlit görüntüleyici. build context = services/viewer/
set -euo pipefail
REGION="${REGION:-europe-west3}"
gcloud run deploy quakemlab-viewer --source services/viewer --region "$REGION" \
  --cpu 1 --memory 4Gi --timeout 300
