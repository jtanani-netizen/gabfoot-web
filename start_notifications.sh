#!/usr/bin/env bash
set -euo pipefail

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$WORKDIR/.cache/interesting_matches.pid"
LOG_FILE="$WORKDIR/.cache/interesting_matches.log"
PYTHON_BIN="$WORKDIR/.venv/bin/python"

mkdir -p "$WORKDIR/.cache"
cd "$WORKDIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python du venv introuvable: $PYTHON_BIN"
  exit 1
fi

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Le service tourne deja avec PID $(cat "$PID_FILE")"
  exit 0
fi

nohup "$PYTHON_BIN" "$WORKDIR/prediction_reporting_service.py" --loop --limit 3 --min-percent 78 --prediction-every-hours 3 --report-every-hours 1 --daily-report-hour 23 >>"$LOG_FILE" 2>&1 &
PID="$!"
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  echo "Echec du lancement"
  exit 1
fi
echo "$PID" > "$PID_FILE"
echo "Service lance avec PID $PID"
