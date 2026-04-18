#!/bin/bash
# ============================================================
#  KLH cron jobs — runs every 2 hours via launchd (macOS) or
#  Task Scheduler (Windows).
#
#  Jobs executed per run:
#    1. send_tier_offers.py       — tier-aware SOTIB refresh
#    2. daily_ad_housekeeping.py  — re-tier ads on price band changes
#
#  Logs append to ~/.klh/cron_logs/<YYYY-MM-DD>.log (one file per day).
#  Errors don't halt the other job — each wrapped in its own try block.
# ============================================================

set -u  # unset var = error, but don't -e so one job failing doesn't kill the other

REPO="/Volumes/Samsung_990_4TB/KLH/klh-listing-tool"
VENV="${REPO}/.venv"
LOG_DIR="${HOME}/.klh/cron_logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/$(date +%Y-%m-%d).log"

echo "" >> "${LOG_FILE}"
echo "=== KLH cron run at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "${LOG_FILE}"

cd "${REPO}" || { echo "ERROR: cd ${REPO} failed" >> "${LOG_FILE}"; exit 1; }

# shellcheck disable=SC1091
source "${VENV}/bin/activate"

# --- Job 1: tier-aware SOTIB offer refresh -------------------------------- #
echo "--- send_tier_offers ---" >> "${LOG_FILE}"
if ! python scripts/send_tier_offers.py --apply --yes >> "${LOG_FILE}" 2>&1; then
    echo "WARN: send_tier_offers exited non-zero" >> "${LOG_FILE}"
fi

# --- Job 2: daily ad-tier reconciler -------------------------------------- #
echo "--- daily_ad_housekeeping ---" >> "${LOG_FILE}"
if ! python scripts/daily_ad_housekeeping.py --apply >> "${LOG_FILE}" 2>&1; then
    echo "WARN: daily_ad_housekeeping exited non-zero" >> "${LOG_FILE}"
fi

echo "=== KLH cron run done at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "${LOG_FILE}"
