#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests


ROOT = Path("/home/jibril")
DEFAULT_APK = Path("/home/jibril/gabfoot-web-upload-20260316/dist/GABFOOT.apk")


def read_env_value(key: str) -> str:
    for env_path in [
        ROOT / "gabfoot-web-upload-20260316" / ".env",
        ROOT / "match_analyzer" / ".env",
    ]:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    return os.getenv(key, "").strip()


def main() -> int:
    apk_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_APK
    if not apk_path.exists():
        print(f"APK introuvable: {apk_path}", file=sys.stderr)
        return 1

    token = read_env_value("TELEGRAM_BOT_TOKEN")
    chat_id = read_env_value("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant", file=sys.stderr)
        return 1

    caption = (
        "GABFOOT Android APK\n"
        "Version WebView connectee au site premium.\n"
        "Si Android bloque l'installation, autorise les APK inconnus puis relance."
    )
    with apk_path.open("rb") as fh:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (apk_path.name, fh, "application/vnd.android.package-archive")},
            timeout=120,
        )
    print(response.status_code)
    print(response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
