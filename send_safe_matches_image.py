#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from notify_interesting_matches import DATA_CACHE, ENV_FILE, collect_interesting_matches, save_data_cache
from project_paths import CARDS_DIR
from send_demo_model_style import generate_card


@dataclass
class CardMatch:
    home: str
    away: str
    tournament: str
    date: datetime
    hg: int
    ag: int
    ph: int
    pa: int
    ht: str
    ht_pick: str
    exact: str
    score2: str
    score3: str
    winner: str
    dc: str
    over25: str


def parse_record_points(record: str) -> int:
    total = 0
    for part in record.split():
        if part.endswith("G"):
            total += int(part[:-1]) * 3
        elif part.endswith("N"):
            total += int(part[:-1])
    return total


def probable_scores(match) -> tuple[str, str, str]:
    if match.prediction == "1":
        return ("2-0", "2-1", "1-0")
    if match.prediction == "2":
        return ("0-2", "1-2", "0-1")
    if match.prediction == "1X":
        return ("1-0", "1-1", "2-1")
    if match.prediction == "X2":
        return ("0-1", "1-1", "1-2")
    return ("1-1", "1-0", "0-1")


def probable_half_time(main_score: str) -> str:
    home, away = [int(x) for x in main_score.split("-")]
    return f"{home // 2}-{away // 2}"


def half_time_winner(main_score: str, home_name: str, away_name: str) -> str:
    home, away = [int(x) for x in main_score.split("-")]
    if home > away:
        return home_name
    if away > home:
        return away_name
    return "Nul"


def double_chance(prediction: str) -> str:
    return {"1": "1X", "2": "X2", "1X": "1X", "X2": "X2"}.get(prediction, "12")


def over25(prediction: str, main_score: str) -> str:
    home, away = [int(x) for x in main_score.split("-")]
    return "Oui" if home + away >= 3 else "Non"


def to_card_match(match) -> CardMatch:
    s1, s2, s3 = probable_scores(match)
    exact = s1
    ht = half_time_winner(probable_half_time(exact), match.home_name, match.away_name)
    winner = match.home_name if match.prediction in {"1", "1X"} and exact != "1-1" else (
        match.away_name if match.prediction in {"2", "X2"} and exact != "1-1" else "Nul"
    )
    ph, pa = [int(x) for x in exact.split("-")]
    return CardMatch(
        home=match.home_name,
        away=match.away_name,
        tournament=match.tournament_name,
        date=datetime.fromisoformat(match.kickoff_utc.replace("Z", "+00:00")),
        hg=min(95, max(40, 40 + match.home_points * 4)),
        ag=min(95, max(40, 40 + match.away_points * 4)),
        ph=ph,
        pa=pa,
        ht=ht,
        ht_pick=match.prediction,
        exact=exact,
        score2=s2,
        score3=s3,
        winner=winner,
        dc=double_chance(match.prediction),
        over25=over25(match.prediction, exact),
    )


def send_photo(path: Path, caption: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant")
    with path.open("rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            files={"photo": (path.name, f)},
            data={"chat_id": chat_id, "caption": caption},
            timeout=60,
        )
    r.raise_for_status()


def run_once(limit: int, min_percent: int) -> bool:
    matches = collect_interesting_matches(limit=limit, min_percent=min_percent)
    if not matches:
        save_data_cache(DATA_CACHE)
        return False
    batch = [to_card_match(match) for match in matches[:6]]
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    out = CARDS_DIR / "safe_matches_card.png"
    generate_card(batch, title=f"Matchs surs {min_percent}%+", out_path=str(out))
    send_photo(out, f"Top du top | Matchs surs {min_percent}%+")
    save_data_cache(DATA_CACHE)
    return True


def main() -> int:
    load_dotenv(ENV_FILE)
    parser = argparse.ArgumentParser(description="Envoie les matchs surs en affiche image.")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--min-percent", type=int, default=78)
    parser.add_argument("--every-hours", type=int, default=3)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()

    if args.loop:
        while True:
            try:
                sent = run_once(limit=args.limit, min_percent=args.min_percent)
                print(f"[{datetime.now().isoformat()}] sent={sent}", flush=True)
            except Exception as exc:
                print(f"[{datetime.now().isoformat()}] error={exc}", flush=True)
            time.sleep(max(1, args.every_hours) * 3600)
    else:
        sent = run_once(limit=args.limit, min_percent=args.min_percent)
        print("sent" if sent else "skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
