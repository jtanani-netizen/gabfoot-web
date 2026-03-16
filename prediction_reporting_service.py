#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from analyze import FOTMOB_BASE, HEADERS, get_json
from notify_interesting_matches import ENV_FILE, InterestingMatch, collect_interesting_matches
from send_demo_model_style import generate_card
from send_safe_matches_image import CARDS_DIR, send_photo, to_card_match


STATE_FILE = Path(__file__).resolve().parent / ".cache" / "prediction_reporting_state.json"
CARD_PATH = CARDS_DIR / "scheduled_safe_matches_card.png"
LEAGUE_CACHE_TTL_MINUTES = 15
LEAGUE_FIXTURES_CACHE: dict[int, tuple[datetime, list[dict[str, Any]]]] = {}


def ensure_cache_dir() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CARDS_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, Any]:
    ensure_cache_dir()
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "last_prediction_signature": "",
        "last_prediction_sent_at": "",
        "last_hourly_report_hour": "",
        "last_daily_report_date": "",
        "last_weekly_report_key": "",
        "predictions": [],
    }


def save_state(state: dict[str, Any]) -> None:
    ensure_cache_dir()
    state["predictions"] = state.get("predictions", [])[-400:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2))


def local_now() -> datetime:
    return datetime.now().astimezone()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def should_send_predictions(state: dict[str, Any], every_hours: int, force: bool) -> bool:
    if force:
        return True
    last_sent = parse_dt(state.get("last_prediction_sent_at"))
    if last_sent is None:
        return True
    return local_now() - last_sent >= timedelta(hours=max(1, every_hours))


def build_signature(matches: list[InterestingMatch], min_percent: int) -> str:
    return f"{min_percent}|" + "|".join(f"{item.match_id}:{item.sureness_percent}:{item.prediction}" for item in matches)


def record_predictions(state: dict[str, Any], matches: list[InterestingMatch], sent_at: datetime) -> None:
    batch_id = sent_at.isoformat()
    records = state.get("predictions", [])
    existing_keys = {(item.get("match_id"), item.get("kickoff_utc"), item.get("prediction")) for item in records}
    for match in matches:
        key = (match.match_id, match.kickoff_utc, match.prediction)
        if key in existing_keys:
            continue
        records.append(
            {
                "batch_id": batch_id,
                "sent_at": sent_at.isoformat(),
                "match_id": match.match_id,
                "league_id": match.league_id,
                "kickoff_utc": match.kickoff_utc,
                "page_url": match.page_url,
                "home_name": match.home_name,
                "away_name": match.away_name,
                "prediction": match.prediction,
                "sureness_percent": match.sureness_percent,
                "status": "pending",
                "actual_result": "",
                "score_str": "",
                "settled_at": "",
                "won": None,
            }
        )
    state["predictions"] = records


def result_code_from_score(score_str: str) -> str | None:
    parts = [part.strip() for part in score_str.split("-")]
    if len(parts) != 2:
        return None
    try:
        home = int(parts[0])
        away = int(parts[1])
    except ValueError:
        return None
    if home > away:
        return "1"
    if away > home:
        return "2"
    return "X"


def prediction_is_correct(prediction: str, actual: str) -> bool:
    if prediction == "1":
        return actual == "1"
    if prediction == "2":
        return actual == "2"
    if prediction == "X":
        return actual == "X"
    if prediction == "1X":
        return actual in {"1", "X"}
    if prediction == "X2":
        return actual in {"X", "2"}
    if prediction == "12":
        return actual in {"1", "2"}
    return False


def league_fixtures(league_id: int) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    cached = LEAGUE_FIXTURES_CACHE.get(league_id)
    if cached and now - cached[0] <= timedelta(minutes=LEAGUE_CACHE_TTL_MINUTES):
        return cached[1]
    payload = get_json(f"{FOTMOB_BASE}/api/leagues?id={league_id}", timeout=40)
    fixtures = payload.get("fixtures", {}).get("allMatches", [])
    LEAGUE_FIXTURES_CACHE[league_id] = (now, fixtures)
    return fixtures


def fetch_match_outcome(league_id: int, match_id: int) -> dict[str, Any] | None:
    try:
        fixtures = league_fixtures(league_id)
    except Exception:
        return None
    match_id_str = str(match_id)
    for item in fixtures:
        if str(item.get("id")) != match_id_str:
            continue
        status = item.get("status", {})
        if status.get("cancelled") or status.get("awarded"):
            return {"state": "void"}
        if not status.get("finished"):
            return {"state": "pending"}
        score_str = str(status.get("scoreStr", "")).replace(" ", "")
        actual = result_code_from_score(score_str)
        if not actual:
            return {"state": "pending"}
        return {
            "state": "finished",
            "actual_result": actual,
            "score_str": score_str,
        }
    return None


def settle_predictions(state: dict[str, Any]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    settled_now: list[dict[str, Any]] = []
    for item in state.get("predictions", []):
        if item.get("status") != "pending":
            continue
        kickoff = parse_dt(str(item.get("kickoff_utc")))
        if kickoff and kickoff > now + timedelta(minutes=5):
            continue
        outcome = fetch_match_outcome(int(item.get("league_id", 0)), int(item.get("match_id", 0)))
        if not outcome:
            continue
        if outcome["state"] == "pending":
            continue
        if outcome["state"] == "void":
            item["status"] = "void"
            item["settled_at"] = now.isoformat()
            settled_now.append(item)
            continue
        item["status"] = "settled"
        item["actual_result"] = outcome["actual_result"]
        item["score_str"] = outcome["score_str"]
        item["settled_at"] = now.isoformat()
        item["won"] = prediction_is_correct(str(item.get("prediction", "")), str(item.get("actual_result", "")))
        settled_now.append(item)
    return settled_now


def filter_settled(records: list[dict[str, Any]], start_dt: datetime | None = None, end_dt: datetime | None = None) -> list[dict[str, Any]]:
    out = []
    for item in records:
        if item.get("status") != "settled":
            continue
        settled_at = parse_dt(str(item.get("settled_at")))
        if settled_at is None:
            continue
        if start_dt and settled_at < start_dt:
            continue
        if end_dt and settled_at >= end_dt:
            continue
        out.append(item)
    return out


def stats_for(records: list[dict[str, Any]]) -> tuple[int, int, int]:
    total = len(records)
    wins = sum(1 for item in records if item.get("won") is True)
    losses = total - wins
    return total, wins, losses


def pct(wins: int, total: int) -> int:
    if total <= 0:
        return 0
    return round(wins * 100 / total)


def format_result_line(item: dict[str, Any]) -> str:
    verdict = "BON" if item.get("won") else "FAUX"
    return (
        f"{html.escape(str(item.get('home_name')))} vs {html.escape(str(item.get('away_name')))}"
        f" | Pick <b>{html.escape(str(item.get('prediction')))}</b>"
        f" | Score <b>{html.escape(str(item.get('score_str') or '-'))}</b>"
        f" | Resultat <b>{verdict}</b>"
        f" | Taux <b>{int(item.get('sureness_percent', 0))}%</b>"
    )


def format_hourly_report(newly_settled: list[dict[str, Any]], last_24h: list[dict[str, Any]]) -> str:
    now_label = local_now().strftime("%d/%m/%Y %H:%M")
    total_now, wins_now, losses_now = stats_for(newly_settled)
    total_24h, wins_24h, losses_24h = stats_for(last_24h)
    lines = [
        "<b>GABFOOT | Rapport horaire</b>",
        f"Mise a jour: {now_label}",
        f"Matchs finalises cette heure: <b>{total_now}</b>",
        f"Bons: <b>{wins_now}</b> | Faux: <b>{losses_now}</b> | Reussite: <b>{pct(wins_now, total_now)}%</b>",
        f"Dernieres 24h: <b>{wins_24h}/{total_24h}</b> | Reussite: <b>{pct(wins_24h, total_24h)}%</b>",
        "",
    ]
    for item in newly_settled[:8]:
        lines.append(format_result_line(item))
    return "\n".join(lines).strip()


def format_daily_report(day: datetime, daily_records: list[dict[str, Any]], all_time_records: list[dict[str, Any]]) -> str:
    total_day, wins_day, losses_day = stats_for(daily_records)
    total_all, wins_all, losses_all = stats_for(all_time_records)
    day_label = day.strftime("%d/%m/%Y")
    lines = [
        "<b>GABFOOT | Rapport quotidien</b>",
        f"Jour: {day_label}",
        f"Bons: <b>{wins_day}</b> | Faux: <b>{losses_day}</b> | Reussite du jour: <b>{pct(wins_day, total_day)}%</b>",
        f"Historique global: <b>{wins_all}/{total_all}</b> | Reussite globale: <b>{pct(wins_all, total_all)}%</b>",
        "",
    ]
    top_records = sorted(daily_records, key=lambda item: int(item.get("sureness_percent", 0)), reverse=True)[:10]
    for item in top_records:
        lines.append(format_result_line(item))
    return "\n".join(lines).strip()


def format_weekly_report(week_key: str, weekly_records: list[dict[str, Any]], all_time_records: list[dict[str, Any]]) -> str:
    total_week, wins_week, losses_week = stats_for(weekly_records)
    total_all, wins_all, losses_all = stats_for(all_time_records)
    lines = [
        "<b>GABFOOT | Rapport hebdomadaire</b>",
        f"Semaine: {week_key}",
        f"Bons: <b>{wins_week}</b> | Faux: <b>{losses_week}</b> | Reussite semaine: <b>{pct(wins_week, total_week)}%</b>",
        f"Historique global: <b>{wins_all}/{total_all}</b> | Reussite globale: <b>{pct(wins_all, total_all)}%</b>",
        "",
    ]
    top_records = sorted(weekly_records, key=lambda item: int(item.get("sureness_percent", 0)), reverse=True)[:12]
    for item in top_records:
        lines.append(format_result_line(item))
    return "\n".join(lines).strip()


def send_telegram_html(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant dans .env")
    import requests

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()


def send_prediction_batch(state: dict[str, Any], limit: int, min_percent: int, force: bool) -> bool:
    matches = collect_interesting_matches(limit=limit, min_percent=min_percent)
    if not matches:
        return False
    signature = build_signature(matches, min_percent)
    if not force and signature == state.get("last_prediction_signature"):
        return False
    batch = [to_card_match(match) for match in matches[:6]]
    generate_card(batch, title=f"Matchs surs {min_percent}%+", out_path=str(CARD_PATH))
    send_photo(CARD_PATH, f"Top du top | Matchs surs {min_percent}%+")
    sent_at = local_now()
    record_predictions(state, matches, sent_at)
    state["last_prediction_signature"] = signature
    state["last_prediction_sent_at"] = sent_at.isoformat()
    return True


def maybe_send_hourly_report(state: dict[str, Any], newly_settled: list[dict[str, Any]]) -> bool:
    if not newly_settled:
        return False
    hour_key = local_now().strftime("%Y-%m-%d %H")
    if state.get("last_hourly_report_hour") == hour_key:
        return False
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=24)
    last_24h = filter_settled(state.get("predictions", []), start_dt=start_dt, end_dt=end_dt)
    send_telegram_html(format_hourly_report(newly_settled, last_24h))
    state["last_hourly_report_hour"] = hour_key
    return True


def maybe_send_daily_report(state: dict[str, Any], report_hour: int) -> bool:
    now_local = local_now()
    if now_local.hour < report_hour:
        return False
    day_key = now_local.strftime("%Y-%m-%d")
    if state.get("last_daily_report_date") == day_key:
        return False
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    daily_records = filter_settled(state.get("predictions", []), start_dt=start_utc, end_dt=end_utc)
    if not daily_records:
        return False
    all_time = filter_settled(state.get("predictions", []))
    send_telegram_html(format_daily_report(start_local, daily_records, all_time))
    state["last_daily_report_date"] = day_key
    return True


def maybe_send_weekly_report(state: dict[str, Any], report_hour: int) -> bool:
    now_local = local_now()
    if now_local.hour < report_hour or now_local.weekday() != 6:
        return False
    iso_year, iso_week, _ = now_local.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    if state.get("last_weekly_report_key") == week_key:
        return False
    start_local = (now_local - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = start_local - timedelta(days=start_local.weekday())
    end_local = start_local + timedelta(days=7)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    weekly_records = filter_settled(state.get("predictions", []), start_dt=start_utc, end_dt=end_utc)
    if not weekly_records:
        return False
    all_time = filter_settled(state.get("predictions", []))
    send_telegram_html(format_weekly_report(week_key, weekly_records, all_time))
    state["last_weekly_report_key"] = week_key
    return True


def cleanup_old_predictions(state: dict[str, Any], keep_days: int = 14) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    kept = []
    for item in state.get("predictions", []):
        kickoff = parse_dt(str(item.get("kickoff_utc")))
        settled_at = parse_dt(str(item.get("settled_at")))
        anchor = settled_at or kickoff
        if anchor is None or anchor >= cutoff:
            kept.append(item)
    state["predictions"] = kept


def run_cycle(
    *,
    limit: int,
    min_percent: int,
    prediction_every_hours: int,
    daily_report_hour: int,
    force_predictions: bool = False,
) -> dict[str, bool]:
    state = load_state()
    sent_predictions = False
    if should_send_predictions(state, prediction_every_hours, force_predictions):
        try:
            sent_predictions = send_prediction_batch(state, limit, min_percent, force_predictions)
        except Exception:
            sent_predictions = False
    newly_settled = settle_predictions(state)
    sent_hourly = maybe_send_hourly_report(state, newly_settled)
    sent_daily = maybe_send_daily_report(state, daily_report_hour)
    sent_weekly = maybe_send_weekly_report(state, daily_report_hour)
    cleanup_old_predictions(state)
    save_state(state)
    return {
        "sent_predictions": sent_predictions,
        "sent_hourly": sent_hourly,
        "sent_daily": sent_daily,
        "sent_weekly": sent_weekly,
        "settled": bool(newly_settled),
    }


def main() -> int:
    load_dotenv(ENV_FILE)
    parser = argparse.ArgumentParser(description="Service GABFOOT: affiches, suivi des resultats et rapports Telegram.")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--min-percent", type=int, default=78)
    parser.add_argument("--prediction-every-hours", type=int, default=3)
    parser.add_argument("--report-every-hours", type=int, default=1)
    parser.add_argument("--daily-report-hour", type=int, default=23)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--force-predictions", action="store_true")
    args = parser.parse_args()

    if args.loop:
        while True:
            try:
                result = run_cycle(
                    limit=args.limit,
                    min_percent=args.min_percent,
                    prediction_every_hours=args.prediction_every_hours,
                    daily_report_hour=args.daily_report_hour,
                    force_predictions=args.force_predictions,
                )
                print(f"[{datetime.now().isoformat()}] {result}", flush=True)
            except Exception as exc:
                print(f"[{datetime.now().isoformat()}] error={exc}", flush=True)
            time.sleep(max(1, args.report_every_hours) * 3600)
    else:
        result = run_cycle(
            limit=args.limit,
            min_percent=args.min_percent,
            prediction_every_hours=args.prediction_every_hours,
            daily_report_hour=args.daily_report_hour,
            force_predictions=args.force_predictions,
        )
        print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
