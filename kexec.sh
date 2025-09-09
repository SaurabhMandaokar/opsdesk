#!/usr/bin/env bash
set -euo pipefail

# ---- edit these once ----
ZONE="asia-northeast1-a"
VM="kubectl"
PROJECT="spdb-pipe-prod"
# -------------------------

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 <command...>"
  echo "Example: $0 kubectl get pods -A"
  exit 1
fi

# Safely quote the user command for remote bash -lc
REMOTE_CMD=$(printf "%q " "$@")

gcloud compute ssh "${VM}" \
  --zone "${ZONE}" \
  --tunnel-through-iap \
  --command "bash -lc 'gcloud config set project ${PROJECT} >/dev/null; ${REMOTE_CMD}'"

