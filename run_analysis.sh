#!/usr/bin/env bash
set -euo pipefail

# configure teams via env or defaults
TEAM_A="${TEAM_A:-PSG}"
TEAM_B="${TEAM_B:-Marseille}"
LAST_N="${LAST_N:-5}"

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$WORKDIR/automation.log"

if [[ ! -f "$WORKDIR/.venv/bin/activate" ]]; then
  echo "Virtualenv not found in $WORKDIR/.venv. Initialise avec 'python3 -m venv .venv'." >&2
  exit 1
fi

source "$WORKDIR/.venv/bin/activate"

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Lancement de l'analyse $TEAM_A vs $TEAM_B (last $LAST_N)" | tee -a "$LOG_FILE"
python "$WORKDIR/analyze.py" "$TEAM_A" "$TEAM_B" "$LAST_N" >>"$LOG_FILE" 2>&1
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Analyse terminée" >>"$LOG_FILE"
