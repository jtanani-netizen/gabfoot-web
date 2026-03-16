#!/usr/bin/env bash
set -euo pipefail

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$WORKDIR/.cache/interesting_matches.pid"

if [[ ! -f "$PID_FILE" ]]; then
  PID="$(pgrep -n -f "$WORKDIR/prediction_reporting_service.py --loop" || true)"
  if [[ -z "$PID" ]]; then
    echo "Aucun PID trouve"
    exit 0
  fi
else
  PID="$(cat "$PID_FILE")"
fi

if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Service arrete: $PID"
else
  echo "Processus deja arrete"
fi

rm -f "$PID_FILE"
