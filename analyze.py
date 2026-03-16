#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import textwrap
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


HEADERS = {"User-Agent": "Mozilla/5.0"}
FOTMOB_BASE = "https://www.fotmob.com"
SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
TEAM_INDEX_FILE = CACHE_DIR / "team_index.json"


@dataclass
class TeamRef:
    id: int
    name: str
    short_name: str
    league_name: str
    page_url: str


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def get_json(url: str, *, timeout: int = 25) -> Any:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def fetch_popular_leagues() -> list[dict[str, Any]]:
    payload = get_json(f"{FOTMOB_BASE}/api/data/allLeagues")
    leagues = payload.get("popular", [])
    extra_ids = {61, 71}
    seen = {league["id"] for league in leagues}
    for country in payload.get("countries", []):
        for league in country.get("leagues", []):
            if league.get("id") in extra_ids and league.get("id") not in seen:
                leagues.append(league)
                seen.add(league["id"])
    return leagues


def extract_teams_from_league(payload: dict[str, Any]) -> list[TeamRef]:
    refs: dict[int, TeamRef] = {}
    league_name = payload.get("details", {}).get("name", "")
    for block in payload.get("table") or []:
        table = block.get("data", {}).get("table", {})
        for section_name in ("all", "home", "away"):
            for row in table.get(section_name, []):
                team_id = row.get("id")
                if not team_id:
                    continue
                refs[team_id] = TeamRef(
                    id=int(team_id),
                    name=row.get("name", ""),
                    short_name=row.get("shortName", ""),
                    league_name=league_name,
                    page_url=row.get("pageUrl", ""),
                )
    return list(refs.values())


def build_team_index(force: bool = False) -> list[TeamRef]:
    ensure_cache_dir()
    if TEAM_INDEX_FILE.exists() and not force:
        raw = json.loads(TEAM_INDEX_FILE.read_text())
        return [TeamRef(**item) for item in raw]

    refs: dict[int, TeamRef] = {}
    for league in fetch_popular_leagues():
        league_id = league["id"]
        payload = get_json(f"{FOTMOB_BASE}/api/leagues?id={league_id}", timeout=35)
        if not payload.get("table"):
            continue
        for ref in extract_teams_from_league(payload):
            refs[ref.id] = ref

    items = [ref.__dict__ for ref in sorted(refs.values(), key=lambda x: (x.league_name, x.name))]
    TEAM_INDEX_FILE.write_text(json.dumps(items, ensure_ascii=True, indent=2))
    return [TeamRef(**item) for item in items]


def resolve_team(query: str, refs: list[TeamRef]) -> TeamRef:
    norm_query = normalize(query)
    exact_candidates = [
        ref
        for ref in refs
        if norm_query in {normalize(ref.name), normalize(ref.short_name)}
        or norm_query == normalize(ref.name).replace("saint", "st")
    ]
    if exact_candidates:
        return exact_candidates[0]

    aliases: dict[str, TeamRef] = {}
    for ref in refs:
        for variant in {ref.name, ref.short_name, f"{ref.name} {ref.league_name}"}:
            norm = normalize(variant)
            if norm:
                aliases[norm] = ref

    best = difflib.get_close_matches(norm_query, aliases.keys(), n=1, cutoff=0.55)
    if best:
        return aliases[best[0]]

    raise SystemExit(f"Equipe introuvable: {query}")


def result_letter(value: int | None) -> str:
    mapping = {1: "G", 0: "N", -1: "P"}
    return mapping.get(value, "?")


def parse_form(team_payload: dict[str, Any]) -> dict[str, Any]:
    matches = []
    wins = draws = losses = gf = ga = 0
    for item in team_payload.get("overview", {}).get("teamForm", []):
        tooltip = item.get("tooltipText", {})
        home_id = tooltip.get("homeTeamId")
        away_id = tooltip.get("awayTeamId")
        our_id = team_payload["details"]["id"]
        home_score = tooltip.get("homeScore", 0) or 0
        away_score = tooltip.get("awayScore", 0) or 0
        if our_id == home_id:
            gf += home_score
            ga += away_score
        else:
            gf += away_score
            ga += home_score

        outcome = item.get("result")
        if outcome == 1:
            wins += 1
        elif outcome == 0:
            draws += 1
        elif outcome == -1:
            losses += 1

        matches.append(
            {
                "result": result_letter(outcome),
                "score": item.get("score", "?"),
                "opponent": tooltip.get("awayTeam") if our_id == home_id else tooltip.get("homeTeam"),
                "competition": item.get("tournamentName", ""),
                "utc": tooltip.get("utcTime") or item.get("date", {}).get("utcTime"),
            }
        )

    return {
        "record": f"{wins}G {draws}N {losses}P",
        "goals": f"{gf} pour / {ga} contre",
        "matches": matches,
    }


def parse_standing(team_payload: dict[str, Any]) -> str:
    team_id = team_payload["details"]["id"]
    for block in team_payload.get("table", []):
        table = block.get("data", {}).get("table", {})
        for row in table.get("all", []):
            if int(row.get("id", 0)) == team_id:
                return (
                    f"{block['data'].get('leagueName', '')}: "
                    f"{row.get('idx')}e, {row.get('pts')} pts, "
                    f"{row.get('scoresStr')} ({row.get('played')} matchs)"
                )
    return "Classement indisponible"


def parse_top_players(team_payload: dict[str, Any]) -> list[str]:
    top = team_payload.get("overview", {}).get("topPlayers", {})
    lines: list[str] = []
    for key, label in (
        ("byRating", "Meilleure note"),
        ("byGoals", "Buteur"),
        ("byAssists", "Passeur"),
    ):
        bucket = top.get(key, {})
        players = bucket.get("players", [])
        if not players:
            continue
        player = players[0]
        value = player.get("value")
        lines.append(f"{label}: {player.get('name')} ({value})")
    return lines


def parse_lineup_watch(team_payload: dict[str, Any]) -> list[str]:
    lineup = team_payload.get("overview", {}).get("lastLineupStats") or {}
    starters = lineup.get("starters") or []
    ranked = []
    for starter in starters:
        perf = starter.get("performance") or {}
        ranked.append(
            (
                perf.get("seasonRating") or 0,
                f"{starter.get('name')} note saison {perf.get('seasonRating', '?')} "
                f"buts {perf.get('seasonGoals', 0)} passes {perf.get('seasonAssists', 0)}",
            )
        )
    ranked.sort(reverse=True)
    return [item[1] for item in ranked[:3]]


def parse_news(team_payload: dict[str, Any]) -> list[str]:
    items = team_payload.get("overview", {}).get("newsSummary", {}).get("items", [])
    lines = []
    for item in items[:3]:
        source = item.get("source", {})
        title = source.get("title", "Actu")
        summary = item.get("summary", "")
        uri = source.get("uri", "")
        lines.append(f"{title} - {summary} {uri}".strip())
    return lines


def team_summary(ref: TeamRef) -> dict[str, Any]:
    payload = get_json(f"{FOTMOB_BASE}/api/teams?id={ref.id}", timeout=35)
    return {
        "ref": ref,
        "standing": parse_standing(payload),
        "form": parse_form(payload),
        "top_players": parse_top_players(payload),
        "lineup_watch": parse_lineup_watch(payload),
        "news": parse_news(payload),
        "next_match": payload.get("overview", {}).get("nextMatch"),
    }


def sportsdb_team_search(name: str) -> dict[str, Any] | None:
    payload = get_json(f"{SPORTSDB_BASE}/searchteams.php?t={requests.utils.quote(name)}")
    teams = payload.get("teams") or []
    for team in teams:
        if team.get("strSport") == "Soccer":
            return team
    return None


def fetch_h2h(home_name: str, away_name: str) -> list[dict[str, Any]]:
    query = f"{home_name}_vs_{away_name}"
    payload = get_json(f"{SPORTSDB_BASE}/searchevents.php?e={query}")
    events = payload.get("event") or []
    filtered = []
    wanted = {normalize(home_name), normalize(away_name)}
    for event in events:
        if event.get("strSport") != "Soccer":
            continue
        names = {normalize(event.get("strHomeTeam", "")), normalize(event.get("strAwayTeam", ""))}
        if names == wanted:
            filtered.append(event)
    filtered.sort(key=lambda item: item.get("strTimestamp") or "", reverse=True)
    return filtered[:5]


def format_h2h(events: list[dict[str, Any]]) -> list[str]:
    lines = []
    for event in events:
        score = f"{event.get('intHomeScore', '?')}-{event.get('intAwayScore', '?')}"
        when = event.get("dateEvent") or event.get("strTimestamp", "")[:10]
        league = event.get("strLeague", "")
        lines.append(f"{when} - {event.get('strHomeTeam')} {score} {event.get('strAwayTeam')} ({league})")
    return lines


def format_recent_matches(matches: list[dict[str, Any]]) -> list[str]:
    lines = []
    for match in matches:
        when = (match.get("utc") or "")[:10]
        lines.append(
            f"{when} - {match['result']} vs {match['opponent']} - {match['score']} - {match['competition']}"
        )
    return lines


def compare_forms(home: dict[str, Any], away: dict[str, Any]) -> list[str]:
    notes = []
    home_form = home["form"]["matches"]
    away_form = away["form"]["matches"]
    home_points = sum({"G": 3, "N": 1, "P": 0}.get(item["result"], 0) for item in home_form)
    away_points = sum({"G": 3, "N": 1, "P": 0}.get(item["result"], 0) for item in away_form)
    if home_points > away_points:
        notes.append(f"Forme recente avantage {home['ref'].name}.")
    elif away_points > home_points:
        notes.append(f"Forme recente avantage {away['ref'].name}.")
    else:
        notes.append("Forme recente assez equilibree.")

    home_goals = home["form"]["goals"]
    away_goals = away["form"]["goals"]
    notes.append(f"{home['ref'].name}: {home['form']['record']} ({home_goals}).")
    notes.append(f"{away['ref'].name}: {away['form']['record']} ({away_goals}).")
    return notes


def extract_points(standing: str) -> int:
    match = re.search(r": \d+e, (\d+) pts", standing)
    return int(match.group(1)) if match else 0


def extract_goal_totals(goals_text: str) -> tuple[int, int]:
    match = re.search(r"(\d+) pour / (\d+) contre", goals_text)
    if not match:
        return (0, 0)
    return int(match.group(1)), int(match.group(2))


def predict_outcome(home: dict[str, Any], away: dict[str, Any], h2h: list[dict[str, Any]]) -> list[str]:
    home_points = sum({"G": 3, "N": 1, "P": 0}.get(item["result"], 0) for item in home["form"]["matches"])
    away_points = sum({"G": 3, "N": 1, "P": 0}.get(item["result"], 0) for item in away["form"]["matches"])
    home_rank_points = extract_points(home["standing"])
    away_rank_points = extract_points(away["standing"])

    score_home = home_points + (home_rank_points // 8)
    score_away = away_points + (away_rank_points // 8)

    h2h_home = 0
    h2h_away = 0
    for event in h2h[:5]:
        hs = int(event.get("intHomeScore") or 0)
        aw = int(event.get("intAwayScore") or 0)
        home_name = normalize(event.get("strHomeTeam", ""))
        if home_name == normalize(home["ref"].name):
            if hs > aw:
                h2h_home += 1
            elif hs < aw:
                h2h_away += 1
        else:
            if aw > hs:
                h2h_home += 1
            elif aw < hs:
                h2h_away += 1

    score_home += h2h_home
    score_away += h2h_away

    if score_home - score_away >= 3:
        main_pick = "1"
        confidence = "forte"
    elif score_away - score_home >= 3:
        main_pick = "2"
        confidence = "forte"
    else:
        main_pick = "1X" if score_home >= score_away else "X2"
        confidence = "moyenne"

    home_for, home_against = extract_goal_totals(home["form"]["goals"])
    away_for, away_against = extract_goal_totals(away["form"]["goals"])
    avg_total = (home_for + home_against + away_for + away_against) / 10 if any(
        [home_for, home_against, away_for, away_against]
    ) else 0
    if avg_total >= 2.8:
        goal_note = "Pari buts: Plus de 2.5 buts"
    elif avg_total >= 2.0:
        goal_note = "Pari buts: Plus de 1.5 buts"
    else:
        goal_note = "Pari buts: Moins de 3.5 buts"

    return [
        f"Choix principal: {main_pick}",
        f"Confiance: {confidence}",
        f"Tendance forme: {home['ref'].name} {home_points} pts recents vs {away_points} pour {away['ref'].name}",
        f"Tendance H2H: {home['ref'].name} {h2h_home} victoires, {away['ref'].name} {h2h_away} victoires",
        goal_note,
    ]


def render_report(home: dict[str, Any], away: dict[str, Any], h2h: list[dict[str, Any]]) -> str:
    prediction = predict_outcome(home, away, h2h)
    sections = [
        f"# Analyse pre-match: {home['ref'].name} vs {away['ref'].name}",
        "",
        "## Pronostic",
        *[f"- {line}" for line in prediction],
        "",
        "## Vue rapide",
        *[f"- {line}" for line in compare_forms(home, away)],
        "",
        "## Classement",
        f"- {home['ref'].name}: {home['standing']}",
        f"- {away['ref'].name}: {away['standing']}",
        "",
        f"## {home['ref'].name} - derniers matchs",
        *[f"- {line}" for line in format_recent_matches(home["form"]["matches"])],
        "",
        f"## {away['ref'].name} - derniers matchs",
        *[f"- {line}" for line in format_recent_matches(away["form"]["matches"])],
        "",
        f"## Joueurs a surveiller - {home['ref'].name}",
        *[f"- {line}" for line in (home["top_players"] + home["lineup_watch"])[:6]],
        "",
        f"## Joueurs a surveiller - {away['ref'].name}",
        *[f"- {line}" for line in (away["top_players"] + away["lineup_watch"])[:6]],
        "",
        "## Confrontations directes recentes",
        *([f"- {line}" for line in format_h2h(h2h)] if h2h else ["- Aucune confrontation recente trouvee."]),
        "",
        f"## Actualites - {home['ref'].name}",
        *([f"- {line}" for line in home["news"]] if home["news"] else ["- Pas d'actualites remontees."]),
        "",
        f"## Actualites - {away['ref'].name}",
        *([f"- {line}" for line in away["news"]] if away["news"] else ["- Pas d'actualites remontees."]),
    ]
    return "\n".join(sections).strip() + "\n"


def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message[:4000]},
        headers=HEADERS,
        timeout=25,
    )
    response.raise_for_status()
    return True


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Analyse un match de football en francais.")
    parser.add_argument("home_team", help="Equipe 1")
    parser.add_argument("away_team", help="Equipe 2")
    parser.add_argument("--rebuild-index", action="store_true", help="Reconstruit le cache des equipes")
    parser.add_argument("--telegram", action="store_true", help="Envoie aussi le rapport sur Telegram")
    args = parser.parse_args()

    refs = build_team_index(force=args.rebuild_index)
    home_ref = resolve_team(args.home_team, refs)
    away_ref = resolve_team(args.away_team, refs)
    home = team_summary(home_ref)
    away = team_summary(away_ref)
    h2h = fetch_h2h(home_ref.name, away_ref.name)
    report = render_report(home, away, h2h)
    print(report)

    if args.telegram:
        sent = send_telegram(report)
        if sent:
            print("Rapport envoye sur Telegram.")
        else:
            print("Telegram non configure: .env incomplet.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.HTTPError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        print(f"Erreur HTTP: {exc} {body}", file=sys.stderr)
        raise
