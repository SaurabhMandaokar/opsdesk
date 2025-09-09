#!/usr/bin/env bash
set -euo pipefail

EMAIL="saurabh.mandaokar@rakuten.com"
ZONE="asia-northeast1-a"
VM_NAME="kubectl"
PROJECT_ID="spdb-pipe-prod"

# 1) Do setup on the VM (project + ensure logged in)
REMOTE_SETUP=$(cat <<'EOSH'
set -euo pipefail
EMAIL='${EMAIL}'
PROJECT_ID='${PROJECT_ID}'

echo ">>> Setting project to ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}" >/dev/null

echo ">>> Checking active account"
ACTIVE_ACCT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' || true)"
if [[ "${ACTIVE_ACCT:-}" != "${EMAIL}" ]]; then
  echo ">>> Logging in as ${EMAIL} (device flow; follow URL once)"
  gcloud auth login "${EMAIL}" --no-launch-browser --brief --update-adc
else
  echo ">>> Already logged in as ${EMAIL}"
fi

# Optional: install GKE auth plugin if needed and if apt is available
if command -v apt-get >/dev/null 2>&1; then
  # Component manager is disabled on apt-based installs; use apt instead.
  sudo apt-get update -y >/dev/null 2>&1 || true
  sudo apt-get install -y google-cloud-sdk-gke-gcloud-auth-plugin >/dev/null 2>&1 || true
fi

echo ">>> Setup complete."
EOSH
)

REMOTE_SETUP=${REMOTE_SETUP//'${EMAIL}'/$EMAIL}
REMOTE_SETUP=${REMOTE_SETUP//'${PROJECT_ID}'/$PROJECT_ID}

# Run setup (non-interactive, will return to local after it finishes)
gcloud compute ssh "${VM_NAME}" \
  --zone "${ZONE}" \
  --tunnel-through-iap \
  --command "${REMOTE_SETUP}"

# 2) Now attach an interactive shell and STAY there
# Use exec so this script is replaced by the SSH session and won't "jump back".
exec gcloud compute ssh "${VM_NAME}" \
  --zone "${ZONE}" \
  --tunnel-through-iap
