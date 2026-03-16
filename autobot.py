#!/usr/bin/env python3
"""Pilote unique pour lancer l'analyse et envoyer le rapport Telegram.

- Lit les secrets dans .env ou les variables d'environnement.
- Permet de lister rapidement les chat_id connus via --get-chat-id.
- Relance l'analyse (analyse.analyse) avec les équipes choisies.
"""

import argparse
import os
import sys
import logging
import requests
from dotenv import load_dotenv

from analyze import analyse


def get_chat_ids(token: str):
    """Retourne la liste des chat_id vus par le bot."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    chat_ids = []
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("edited_message")
        if msg and "chat" in msg:
            cid = msg["chat"].get("id")
            if cid is not None and cid not in chat_ids:
                chat_ids.append(cid)
    return chat_ids, data


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Automatisation analyse + Telegram")
    parser.add_argument("--team-a", default=os.getenv("TEAM_A", "PSG"))
    parser.add_argument("--team-b", default=os.getenv("TEAM_B", "Marseille"))
    parser.add_argument("--last-n", type=int, default=int(os.getenv("LAST_N", 5)))
    parser.add_argument("--get-chat-id", action="store_true",
                        help="Liste les chat_id vus par le bot et quitte")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    api_key = os.getenv("API_FOOTBALL_KEY")

    if args.get_chat_id:
        if not token:
            print("Renseigne TELEGRAM_BOT_TOKEN dans .env avant --get-chat-id")
            sys.exit(1)
        try:
            ids, raw = get_chat_ids(token)
            if ids:
                print("chat_id trouvés:", ", ".join(str(c) for c in ids))
            else:
                print("Aucun chat_id trouvé : envoie d'abord un message au bot, puis relance.")
        except Exception as exc:
            print("Échec getUpdates:", exc)
        return

    # Vérifications minimales
    missing = []
    if not api_key:
        missing.append("API_FOOTBALL_KEY")
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not chat_id:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        print("Manque dans .env : " + ", ".join(missing))
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    logging.info("Analyse %s vs %s (last %s)", args.team_a, args.team_b, args.last_n)

    try:
        analyse(args.team_a, args.team_b, args.last_n)
    except Exception as exc:
        logging.error("Échec analyse: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
