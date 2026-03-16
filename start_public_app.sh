#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="$ROOT/.cache"
WEB_PID_FILE="$CACHE_DIR/web_app.pid"
TUNNEL_PID_FILE="$CACHE_DIR/cloudflared.pid"
TUNNEL_LOG="$CACHE_DIR/cloudflared.log"
URL_FILE="$CACHE_DIR/public_url.txt"
PYTHON_BIN="$ROOT/.venv/bin/python"

mkdir -p "$CACHE_DIR"

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
  pgrep -f 'web_app.py' | head -n 1 || true
}

start_web() {
  if [[ -f "$WEB_PID_FILE" ]]; then
    local pid
    pid="$(cat "$WEB_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return
    fi
  fi
  if is_web_ready; then
    local pid
    pid="$(find_running_web_pid)"
    if [[ -n "$pid" ]]; then
      echo "$pid" > "$WEB_PID_FILE"
    fi
    return
  fi
  nohup "$PYTHON_BIN" "$ROOT/web_app.py" >>"$CACHE_DIR/web_app.log" 2>&1 &
  local pid="$!"
  for _ in $(seq 1 20); do
    if is_web_ready; then
      echo "$pid" > "$WEB_PID_FILE"
      return
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  echo "Le serveur web n'a pas demarre"
  exit 1
}

start_tunnel() {
  if [[ -f "$TUNNEL_PID_FILE" ]]; then
    local pid
    pid="$(cat "$TUNNEL_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return
    fi
  fi
  : > "$TUNNEL_LOG"
  local attempt pid
  for attempt in $(seq 1 8); do
    nohup /home/jibril/cloudflared tunnel --url http://127.0.0.1:8012 >>"$TUNNEL_LOG" 2>&1 &
    pid="$!"
    sleep 4
    if kill -0 "$pid" 2>/dev/null; then
      echo "$pid" > "$TUNNEL_PID_FILE"
      return
    fi
    echo "Tentative tunnel $attempt/8 echouee, nouvelle tentative..." >>"$TUNNEL_LOG"
    sleep 2
  done
  echo "Le tunnel Cloudflare n'a pas demarre"
  exit 1
}

extract_url() {
  CACHE_DIR="$CACHE_DIR" "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path
import re
log = Path(os.environ["CACHE_DIR"]) / "cloudflared.log"
txt = log.read_text(errors="ignore") if log.exists() else ""
m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", txt)
print(m.group(0) if m else "")
PY
}

send_url() {
  local url="$1"
  ROOT="$ROOT" "$PYTHON_BIN" - <<PY
import os, requests
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(os.environ["ROOT"]) / ".env")
token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
chat_id=os.getenv('TELEGRAM_CHAT_ID','').strip()
url=${url@Q}
text=(
    'Nouveau lien GABFOOT actif :\n'
    + url +
    '\n\nOuvre ce lien dans Chrome puis ajoute-le a l ecran d accueil si besoin.'
)
r=requests.post(
    f'https://api.telegram.org/bot{token}/sendMessage',
    json={'chat_id': chat_id, 'text': text, 'disable_web_page_preview': False},
    timeout=60,
)
r.raise_for_status()
print('telegram_sent')
PY
}

telegram_is_configured() {
  ROOT="$ROOT" "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(os.environ["ROOT"]) / ".env")
token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
raise SystemExit(0 if token and chat_id else 1)
PY
}

start_web
start_tunnel

URL=""
for _ in $(seq 1 30); do
  URL="$(extract_url)"
  if [[ -n "$URL" ]]; then
    break
  fi
  sleep 2
done

if [[ -z "$URL" ]]; then
  echo "Lien public introuvable"
  exit 1
fi

OLD_URL=""
[[ -f "$URL_FILE" ]] && OLD_URL="$(cat "$URL_FILE")"
echo "$URL" > "$URL_FILE"
echo "Lien public: $URL"

if [[ "$URL" != "$OLD_URL" ]]; then
  if telegram_is_configured; then
    send_url "$URL"
  else
    echo "Telegram non configure, lien non envoye"
  fi
fi
