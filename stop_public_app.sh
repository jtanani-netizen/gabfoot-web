#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="$ROOT/.cache"
for file in "$CACHE_DIR/cloudflared.pid" "$CACHE_DIR/web_app.pid"; do
  if [[ -f "$file" ]]; then
    pid="$(cat "$file")"
    kill "$pid" 2>/dev/null || true
    rm -f "$file"
  fi
done

pkill -f '/home/jibril/cloudflared tunnel --url http://127.0.0.1:8012' 2>/dev/null || true
pkill -f "$ROOT/web_app.py" 2>/dev/null || true
echo "Services arretes"
