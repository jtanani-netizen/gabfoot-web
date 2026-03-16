#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/jibril/gabfoot-web-upload-20260316"
CACHE_DIR="$ROOT/.cache"
URL_FILE="$CACHE_DIR/preferred_public_url.txt"

mkdir -p "$CACHE_DIR"

show_current() {
  if [[ -f "$URL_FILE" ]]; then
    cat "$URL_FILE"
  else
    echo "Aucune URL publique preferee definie."
  fi
}

if [[ "${1:-}" == "--clear" ]]; then
  rm -f "$URL_FILE"
  echo "URL publique preferee supprimee."
  exit 0
fi

if [[ $# -eq 0 ]]; then
  show_current
  echo "Usage: bash $0 https://gabfoot.eu.org"
  echo "       bash $0 --clear"
  exit 0
fi

URL="${1%/}"
if [[ ! "$URL" =~ ^https?:// ]]; then
  echo "URL invalide: utilise une URL complete qui commence par http:// ou https://"
  exit 1
fi

printf '%s\n' "$URL" > "$URL_FILE"
echo "URL publique preferee: $URL"
