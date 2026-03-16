#!/usr/bin/env python3
"""Vérifie l'état des services match_scan et envoie un message Telegram.

Lit API_FOOTBALL_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID depuis .env.
"""

import os
import sys
import subprocess
from datetime import datetime

import site
sys.path.append(site.getusersitepackages())
sys.path.extend([
    "/home/jibril/snap/codex/30/.local/lib/python3.12/site-packages",
    os.path.expanduser("~/.local/lib/python3.12/site-packages"),
])

import requests
from dotenv import load_dotenv


def send_telegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception:
        pass


def unit_status(unit: str):
    def run(cmd):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            return res.stdout.strip() or res.stderr.strip()
        except PermissionError:
            return "perm_denied"
        except FileNotFoundError:
            return "not_available"

    active = run(["systemctl", "--user", "is-active", unit])
    enabled = run(["systemctl", "--user", "is-enabled", unit])
    return active, enabled


def main():
    load_dotenv()
    units = ["match_scan.service", "match_scan.timer"]
    lines = ["Boot check " + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")]
    for u in units:
        active, enabled = unit_status(u)
        lines.append(f"{u}: active={active}, enabled={enabled}")
    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
