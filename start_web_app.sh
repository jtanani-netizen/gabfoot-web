#!/usr/bin/env bash
set -euo pipefail

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$WORKDIR/.cache/web_app.pid"
LOG_FILE="$WORKDIR/.cache/web_app.log"
LOCK_DIR="$WORKDIR/.cache/web_app.lock"
PYTHON_BIN="$WORKDIR/.venv/bin/python"

mkdir -p "$WORKDIR/.cache"
cd "$WORKDIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python du venv introuvable: $PYTHON_BIN"
  exit 1
fi

is_web_ready() {
  "$PYTHON_BIN" - <<'PY'
from urllib.request import urlopen
try:
    with urlopen("http://127.0.0.1:8012", timeout=2) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
}

find_running_web_pid() {
  pgrep -f "${WORKDIR}/web_app.py" | head -n 1 || true
}

cleanup_lock() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

read_pid_file() {
  [[ -f "$PID_FILE" ]] || return 0
  tr -dc '0-9' < "$PID_FILE"
}

write_pid_file() {
  local pid="$1"
  printf '%s\n' "$pid" > "$PID_FILE"
}

clear_stale_pid_file() {
  : > "$PID_FILE"
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Un lancement de la web app est deja en cours"
  exit 1
fi

trap cleanup_lock EXIT

CURRENT_PID="$(read_pid_file)"
if [[ -n "${CURRENT_PID:-}" ]] && kill -0 "$CURRENT_PID" 2>/dev/null; then
  echo "Web app deja lancee avec PID $CURRENT_PID"
  exit 0
fi

clear_stale_pid_file

if is_web_ready; then
  RUNNING_PID="$(find_running_web_pid)"
  if [[ -n "${RUNNING_PID:-}" ]]; then
    write_pid_file "$RUNNING_PID"
  fi
  echo "Web app deja accessible sur http://127.0.0.1:8012"
  exit 0
fi

"$PYTHON_BIN" "$WORKDIR/web_app.py" </dev/null >>"$LOG_FILE" 2>&1 &
PID="$!"

for _ in $(seq 1 20); do
  if is_web_ready; then
    RUNNING_PID="$(find_running_web_pid)"
    write_pid_file "${RUNNING_PID:-$PID}"
    echo "Web app lancee avec PID ${RUNNING_PID:-$PID} sur http://127.0.0.1:8012"
    exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "Echec du lancement"
exit 1
