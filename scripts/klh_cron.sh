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
JOB1_START=$(date +%s)
if python scripts/send_tier_offers.py --apply --yes >> "${LOG_FILE}" 2>&1; then
    JOB1_RESULT="SUCCESS"
else
    JOB1_RESULT="FAILED($?)"
fi
JOB1_DURATION=$(( $(date +%s) - JOB1_START ))
echo "JOB_RESULT send_tier_offers ${JOB1_RESULT} duration=${JOB1_DURATION}s" >> "${LOG_FILE}"

# --- Job 2: daily ad-tier reconciler -------------------------------------- #
echo "--- daily_ad_housekeeping ---" >> "${LOG_FILE}"
JOB2_START=$(date +%s)
if python scripts/daily_ad_housekeeping.py --apply >> "${LOG_FILE}" 2>&1; then
    JOB2_RESULT="SUCCESS"
else
    JOB2_RESULT="FAILED($?)"
fi
JOB2_DURATION=$(( $(date +%s) - JOB2_START ))
echo "JOB_RESULT daily_ad_housekeeping ${JOB2_RESULT} duration=${JOB2_DURATION}s" >> "${LOG_FILE}"

# --- Job 3: daily catch-up sweep — extras + re-cat. Once a day at 07:00. #
JOB3_RESULT="SKIPPED"
JOB3_DURATION=0
if [ "$(date +%H)" = "07" ]; then
    echo "--- daily_listing_repair ---" >> "${LOG_FILE}"
    JOB3_START=$(date +%s)
    if python scripts/daily_listing_repair.py --apply >> "${LOG_FILE}" 2>&1; then
        JOB3_RESULT="SUCCESS"
    else
        JOB3_RESULT="FAILED($?)"
    fi
    JOB3_DURATION=$(( $(date +%s) - JOB3_START ))
    echo "JOB_RESULT daily_listing_repair ${JOB3_RESULT} duration=${JOB3_DURATION}s" >> "${LOG_FILE}"
fi

# Persist a structured row to optimization_log so the /cron-health
# dashboard panel can render it without parsing the text log.
python -c "
from pipeline import audit_db
from datetime import datetime, timezone
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
details = ('send_tier_offers=${JOB1_RESULT}/${JOB1_DURATION}s '
           'daily_ad_housekeeping=${JOB2_RESULT}/${JOB2_DURATION}s '
           'daily_listing_repair=${JOB3_RESULT}/${JOB3_DURATION}s')
with audit_db.connect() as c:
    c.execute('INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)',
              ('CRON_RUN', now, details))
    c.commit()
" 2>>"${LOG_FILE}" || true

echo "=== KLH cron run done at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "${LOG_FILE}"
