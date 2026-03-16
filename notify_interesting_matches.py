#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from analyze import FOTMOB_BASE, HEADERS, get_json, parse_form
from api_football import ApiFootballError


CACHE_DIR = Path(__file__).resolve().parent / ".cache"
STATE_FILE = CACHE_DIR / "interesting_matches_state.json"
DATA_CACHE_FILE = CACHE_DIR / "safe_matches_data_cache.json"
DEFAULT_LEAGUES = [47, 53, 87, 54, 55, 42, 73, 61]
TEAM_PAYLOAD_CACHE: dict[int, dict[str, Any]] = {}
HISTORICAL_CACHE: dict[tuple[int, int], dict[str, float]] = {}
API_FIXTURES_CACHE: dict[tuple[int, int], list[dict[str, Any]]] = {}
H2H_CACHE: dict[tuple[int, int], dict[str, float]] = {}
FIXTURE_ID_CACHE: dict[tuple[int, int, str], int | None] = {}
INJURIES_CACHE: dict[int, float] = {}
ENV_FILE = Path(__file__).resolve().parent / ".env"
POPULAR_LEAGUE_NAMES = {
    "Premier League",
    "Ligue 1",
    "LaLiga",
    "Bundesliga",
    "Serie A",
    "Champions League Final Stage",
    "Champions League",
    "Europa League",
}


@dataclass
class InterestingMatch:
    match_id: int
    league_id: int
    league_name: str
    tournament_name: str
    kickoff_utc: str
    page_url: str
    home_name: str
    away_name: str
    home_id: int
    away_id: int
    home_form: str
    away_form: str
    home_points: int
    away_points: int
    home_history_score: float
    away_history_score: float
    h2h_edge: float
    injuries_edge: float
    home_rank: int | None
    away_rank: int | None
    home_goals_per_match: float | None
    away_goals_per_match: float | None
    home_conceded_per_match: float | None
    away_conceded_per_match: float | None
    home_key_player: str
    away_key_player: str
    prediction: str
    confidence: str
    sureness_percent: int
    consensus_notes: list[str]
    interest_score: float
    why: list[str]


def ensure_cache() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_data_cache() -> dict[str, Any]:
    ensure_cache()
    if DATA_CACHE_FILE.exists():
        try:
            return json.loads(DATA_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_data_cache(cache: dict[str, Any]) -> None:
    ensure_cache()
    DATA_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=True, indent=2))


def cache_get(cache: dict[str, Any], namespace: str, key: str, max_age_hours: int) -> Any | None:
    bucket = cache.get(namespace, {})
    entry = bucket.get(key)
    if not entry:
        return None
    try:
        created = datetime.fromisoformat(entry["ts"])
    except Exception:
        return None
    if datetime.now(timezone.utc) - created > timedelta(hours=max_age_hours):
        return None
    return entry.get("value")


def cache_set(cache: dict[str, Any], namespace: str, key: str, value: Any) -> None:
    cache.setdefault(namespace, {})
    cache[namespace][key] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "value": value,
    }


DATA_CACHE = load_data_cache()


def load_state() -> dict[str, Any]:
    ensure_cache()
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_sent_signature": "", "history": []}


def save_state(state: dict[str, Any]) -> None:
    ensure_cache()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2))


def league_priority(league_id: int) -> int:
    mapping = {
        42: 120,
        73: 90,
        47: 80,
        53: 70,
        87: 70,
        54: 65,
        55: 65,
        61: 60,
    }
    return mapping.get(league_id, 40)


def extract_table_rows(league_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for block in league_payload.get("table") or []:
        rows.extend(block.get("data", {}).get("table", {}).get("all", []))
    dedup: dict[int, dict[str, Any]] = {}
    for row in rows:
        team_id = row.get("id")
        if team_id:
            dedup[int(team_id)] = row
    return list(dedup.values())


def top_teams_for_league(league_id: int, limit: int = 6) -> list[dict[str, Any]]:
    payload = get_json(f"{FOTMOB_BASE}/api/leagues?id={league_id}", timeout=35)
    rows = extract_table_rows(payload)
    rows.sort(key=lambda row: row.get("idx", 999))
    return rows[:limit]


def get_team_payload(team_id: int) -> dict[str, Any]:
    if team_id not in TEAM_PAYLOAD_CACHE:
        TEAM_PAYLOAD_CACHE[team_id] = get_json(f"{FOTMOB_BASE}/api/teams?id={team_id}", timeout=35)
    return TEAM_PAYLOAD_CACHE[team_id]


def recent_points(team_payload: dict[str, Any]) -> int:
    form = parse_form(team_payload)
    score_map = {"G": 3, "N": 1, "P": 0}
    return sum(score_map.get(item["result"], 0) for item in form["matches"])


def team_key_player(team_payload: dict[str, Any]) -> str:
    top = team_payload.get("overview", {}).get("topPlayers") or {}
    for bucket in ("byRating", "byGoals", "byAssists"):
        bucket_payload = top.get(bucket) or {}
        players = bucket_payload.get("players", [])
        if players:
            player = players[0]
            return f"{player.get('name')} ({player.get('value')})"
    return "N/A"


def next_match_stats(next_match: dict[str, Any]) -> dict[str, float | int | None]:
    out: dict[str, float | int | None] = {
        "home_rank": None,
        "away_rank": None,
        "home_goals_per_match": None,
        "away_goals_per_match": None,
        "home_conceded_per_match": None,
        "away_conceded_per_match": None,
    }
    stats_root = next_match.get("stats") or {}
    for stat in stats_root.get("stats", []):
        title = stat.get("title")
        values = stat.get("stats") or []
        if len(values) != 2:
            continue
        if title == "Table position":
            out["home_rank"], out["away_rank"] = values[0], values[1]
        elif title == "Goals per match":
            out["home_goals_per_match"], out["away_goals_per_match"] = values[0], values[1]
        elif title == "Goals conceded per match":
            out["home_conceded_per_match"], out["away_conceded_per_match"] = values[0], values[1]
    return out


def classify_prediction(home_points: int, away_points: int, home_rank: int | None, away_rank: int | None) -> tuple[str, str]:
    score_home = home_points
    score_away = away_points
    if home_rank and away_rank:
        score_home += max(0, 8 - home_rank)
        score_away += max(0, 8 - away_rank)

    delta = score_home - score_away
    if delta >= 4:
        return "1", "forte"
    if delta <= -4:
        return "2", "forte"
    if delta >= 1:
        return "1X", "moyenne"
    if delta <= -1:
        return "X2", "moyenne"
    return "12", "prudente"


def estimate_sureness_percent(
    home_points: int,
    away_points: int,
    home_history_score: float,
    away_history_score: float,
    home_rank: int | None,
    away_rank: int | None,
    prediction: str,
) -> int:
    delta_points = abs(home_points - away_points)
    delta_history = abs(home_history_score - away_history_score)
    rank_edge = abs((away_rank or 10) - (home_rank or 10))
    base = 52
    if prediction in {"1", "2"}:
        base += 8
    elif prediction in {"1X", "X2"}:
        base += 4
    percent = base + delta_points * 3 + rank_edge * 2 + int(delta_history * 12)
    return max(55, min(90, percent))


def attack_defense_edge(
    home_goals_per_match: float | None,
    away_goals_per_match: float | None,
    home_conceded_per_match: float | None,
    away_conceded_per_match: float | None,
) -> tuple[float, float]:
    home_attack = float(home_goals_per_match or 0) - float(away_conceded_per_match or 0)
    away_attack = float(away_goals_per_match or 0) - float(home_conceded_per_match or 0)
    return home_attack, away_attack


def prediction_contradiction_penalty(
    prediction: str,
    home_points: int,
    away_points: int,
    home_history_score: float,
    away_history_score: float,
    home_rank: int | None,
    away_rank: int | None,
    home_attack_edge: float,
    away_attack_edge: float,
) -> int:
    penalty = 0
    if prediction in {"1", "1X"}:
        if away_points > home_points:
            penalty += 5
        if away_history_score > home_history_score:
            penalty += 4
        if away_rank and home_rank and away_rank < home_rank:
            penalty += 3
        if away_attack_edge > home_attack_edge:
            penalty += 3
    if prediction in {"2", "X2"}:
        if home_points > away_points:
            penalty += 5
        if home_history_score > away_history_score:
            penalty += 4
        if away_rank and home_rank and home_rank < away_rank:
            penalty += 3
        if home_attack_edge > away_attack_edge:
            penalty += 3
    return penalty


def improve_prediction(
    prediction: str,
    home_points: int,
    away_points: int,
    home_history_score: float,
    away_history_score: float,
    home_rank: int | None,
    away_rank: int | None,
    home_attack_edge: float,
    away_attack_edge: float,
) -> tuple[str, str]:
    home_strength = home_points + home_history_score * 6 + max(0, 8 - (home_rank or 8)) + home_attack_edge * 3
    away_strength = away_points + away_history_score * 6 + max(0, 8 - (away_rank or 8)) + away_attack_edge * 3
    delta = home_strength - away_strength
    if delta >= 6:
        return "1", "forte"
    if delta <= -6:
        return "2", "forte"
    if delta >= 2:
        return "1X", "moyenne"
    if delta <= -2:
        return "X2", "moyenne"
    return prediction, "prudente"


def explain_interest(
    league_name: str,
    home_rank: int | None,
    away_rank: int | None,
    home_points: int,
    away_points: int,
    tournament_name: str,
) -> list[str]:
    why = []
    if "Champions League" in tournament_name:
        why.append("gros match europeen")
    elif "Europa League" in tournament_name:
        why.append("affiche europeenne")
    if home_rank and away_rank and home_rank <= 6 and away_rank <= 6:
        why.append("duel du haut de tableau")
    if abs(home_points - away_points) <= 2:
        why.append("forme recente tres proche")
    elif home_points >= 10 or away_points >= 10:
        why.append("une equipe arrive en grande forme")
    if not why:
        why.append(f"affiche notable de {league_name}")
    return why[:3]


def build_consensus_notes(
    home_points: int,
    away_points: int,
    home_history_score: float,
    away_history_score: float,
    h2h_edge: float,
    injuries_edge: float,
    home_rank: int | None,
    away_rank: int | None,
    news_count: int,
    prediction: str,
) -> list[str]:
    notes: list[str] = []
    if prediction in {"1", "2"}:
        notes.append("sens du match net")
    elif prediction in {"1X", "X2"}:
        notes.append("double chance solide")
    if abs(home_points - away_points) >= 4:
        notes.append("ecart de forme confirme")
    if abs(home_history_score - away_history_score) >= 0.12:
        notes.append("historique 3 ans favorable")
    if abs(h2h_edge) >= 0.18:
        notes.append("face a face favorable")
    if abs(injuries_edge) >= 0.18:
        notes.append("absences influentes")
    if home_rank and away_rank and abs(home_rank - away_rank) >= 5:
        notes.append("ecart de classement important")
    if news_count:
        notes.append("infos presse disponibles")
    return notes[:4]


def api_get(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not key:
        raise ApiFootballError("API_FOOTBALL_KEY manquante")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "x-apisports-key": key,
    }
    response = requests.get(f"https://v3.football.api-sports.io{path}", headers=headers, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise ApiFootballError(str(payload["errors"]))
    return payload.get("response", [])


def api_team_fixtures(team_id: int, season: int) -> list[dict[str, Any]]:
    key = (team_id, season)
    if key not in API_FIXTURES_CACHE:
        disk_key = f"{team_id}:{season}"
        cached = cache_get(DATA_CACHE, "api_team_fixtures", disk_key, 24 * 7)
        if cached is not None:
            API_FIXTURES_CACHE[key] = cached
        else:
            try:
                API_FIXTURES_CACHE[key] = api_get("/fixtures", {"team": team_id, "season": season})
            except Exception:
                API_FIXTURES_CACHE[key] = []
            cache_set(DATA_CACHE, "api_team_fixtures", disk_key, API_FIXTURES_CACHE[key])
    return API_FIXTURES_CACHE[key]


def find_api_fixture_id(home_id: int, away_id: int, kickoff_utc: str) -> int | None:
    cache_key = (home_id, away_id, kickoff_utc)
    if cache_key in FIXTURE_ID_CACHE:
        return FIXTURE_ID_CACHE[cache_key]
    disk_key = f"{home_id}:{away_id}:{kickoff_utc}"
    cached = cache_get(DATA_CACHE, "fixture_id", disk_key, 24)
    if cached is not None:
        FIXTURE_ID_CACHE[cache_key] = int(cached) if cached else None
        return FIXTURE_ID_CACHE[cache_key]
    season = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00")).year
    fixtures = api_team_fixtures(home_id, season)
    kickoff_dt = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    for item in fixtures:
        teams = item.get("teams", {})
        fixture = item.get("fixture", {})
        if teams.get("home", {}).get("id") != home_id or teams.get("away", {}).get("id") != away_id:
            continue
        fixture_date = fixture.get("date")
        if not fixture_date:
            continue
        fixture_dt = datetime.fromisoformat(fixture_date.replace("Z", "+00:00"))
        if abs((fixture_dt - kickoff_dt).total_seconds()) <= 24 * 3600:
            fixture_id = int(fixture.get("id"))
            FIXTURE_ID_CACHE[cache_key] = fixture_id
            cache_set(DATA_CACHE, "fixture_id", disk_key, fixture_id)
            return fixture_id
    FIXTURE_ID_CACHE[cache_key] = None
    cache_set(DATA_CACHE, "fixture_id", disk_key, 0)
    return None


def h2h_signal(home_id: int, away_id: int) -> float:
    key = tuple(sorted((home_id, away_id)))
    if key in H2H_CACHE:
        return H2H_CACHE[key]["edge"]
    disk_key = f"{key[0]}:{key[1]}"
    cached = cache_get(DATA_CACHE, "h2h_edge", disk_key, 24 * 14)
    if cached is not None:
        H2H_CACHE[key] = {"edge": float(cached)}
        return H2H_CACHE[key]["edge"]
    try:
        items = api_get("/fixtures/headtohead", {"h2h": f"{home_id}-{away_id}"})
    except Exception:
        H2H_CACHE[key] = {"edge": 0.0}
        return 0.0
    recent = sorted(items, key=lambda x: x.get("fixture", {}).get("timestamp", 0), reverse=True)[:10]
    home_score = 0.0
    away_score = 0.0
    for item in recent:
        teams = item.get("teams", {})
        goals = item.get("goals", {})
        h_id = teams.get("home", {}).get("id")
        a_id = teams.get("away", {}).get("id")
        gh = goals.get("home")
        ga = goals.get("away")
        if gh is None or ga is None:
            continue
        if h_id == home_id:
            if gh > ga:
                home_score += 1
            elif gh < ga:
                away_score += 1
            else:
                home_score += 0.4
                away_score += 0.4
        else:
            if ga > gh:
                home_score += 1
            elif ga < gh:
                away_score += 1
            else:
                home_score += 0.4
                away_score += 0.4
    edge = (home_score - away_score) / 10
    H2H_CACHE[key] = {"edge": edge}
    cache_set(DATA_CACHE, "h2h_edge", disk_key, edge)
    return edge


def injuries_signal(api_fixture_id: int | None, home_id: int, away_id: int) -> float:
    if not api_fixture_id:
        return 0.0
    if api_fixture_id in INJURIES_CACHE:
        return INJURIES_CACHE[api_fixture_id]
    disk_key = str(api_fixture_id)
    cached = cache_get(DATA_CACHE, "injuries_edge", disk_key, 12)
    if cached is not None:
        INJURIES_CACHE[api_fixture_id] = float(cached)
        return INJURIES_CACHE[api_fixture_id]
    try:
        items = api_get("/injuries", {"fixture": api_fixture_id})
    except Exception:
        return 0.0
    home_missing = 0.0
    away_missing = 0.0
    for item in items:
        team = item.get("team", {})
        player = item.get("player", {})
        kind = (player.get("type") or "").lower()
        weight = 1.0 if "missing" in kind else 0.5
        if team.get("id") == home_id:
            home_missing += weight
        elif team.get("id") == away_id:
            away_missing += weight
    edge = (away_missing - home_missing) / 5
    INJURIES_CACHE[api_fixture_id] = edge
    cache_set(DATA_CACHE, "injuries_edge", disk_key, edge)
    return edge


def historical_team_score(team_id: int, league_hint: int | None = None) -> float:
    seasons = [2024, 2023, 2022]
    cache_key = (team_id, league_hint or 0)
    if cache_key in HISTORICAL_CACHE:
        return HISTORICAL_CACHE[cache_key]["score"]
    disk_key = f"{team_id}:{league_hint or 0}"
    cached = cache_get(DATA_CACHE, "historical_team_score", disk_key, 24 * 30)
    if cached is not None:
        HISTORICAL_CACHE[cache_key] = {"score": float(cached)}
        return HISTORICAL_CACHE[cache_key]["score"]

    weighted_total = 0.0
    weighted_games = 0.0
    weights = {2024: 1.0, 2023: 0.7, 2022: 0.5}
    for season in seasons:
        params: dict[str, Any] = {"team": team_id, "season": season}
        if league_hint:
            params["league"] = league_hint
        try:
            fixtures = api_get("/fixtures", params)
        except Exception:
            continue
        if not fixtures:
            continue

        season_points = 0
        season_goal_diff = 0
        season_games = 0
        for item in fixtures:
            teams = item.get("teams", {})
            goals = item.get("goals", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            home_id = home.get("id")
            away_id = away.get("id")
            gh = goals.get("home")
            ga = goals.get("away")
            if gh is None or ga is None:
                continue
            season_games += 1
            if team_id == home_id:
                gf, gc = gh, ga
            elif team_id == away_id:
                gf, gc = ga, gh
            else:
                continue
            season_goal_diff += gf - gc
            if gf > gc:
                season_points += 3
            elif gf == gc:
                season_points += 1

        if season_games == 0:
            continue
        points_per_game = season_points / season_games
        gd_per_game = season_goal_diff / season_games
        score = points_per_game / 3 + max(-0.5, min(0.5, gd_per_game / 4))
        weight = weights[season]
        weighted_total += score * weight
        weighted_games += weight

    final_score = weighted_total / weighted_games if weighted_games else 0.5
    HISTORICAL_CACHE[cache_key] = {"score": final_score}
    cache_set(DATA_CACHE, "historical_team_score", disk_key, final_score)
    return final_score


def build_candidate_from_team(team_id: int, league_name: str, league_id: int) -> InterestingMatch | None:
    payload = get_team_payload(team_id)
    next_match = payload.get("overview", {}).get("nextMatch") or {}
    if not next_match or not next_match.get("notStarted"):
        return None

    kickoff = next_match.get("status", {}).get("utcTime")
    if not kickoff:
        return None

    kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if kickoff_dt < now - timedelta(hours=2) or kickoff_dt > now + timedelta(hours=72):
        return None

    stats = next_match_stats(next_match)
    home = next_match.get("home", {})
    away = next_match.get("away", {})
    home_points = recent_points(payload) if home.get("id") == payload["details"]["id"] else 0
    away_payload = get_team_payload(int(away.get("id")))
    away_points = recent_points(away_payload)
    home_history_score = historical_team_score(int(home.get("id")), stats["home_rank"] and next_match.get("tournament", {}).get("leagueId"))
    away_history_score = historical_team_score(int(away.get("id")), stats["away_rank"] and next_match.get("tournament", {}).get("leagueId"))
    api_fixture_id = find_api_fixture_id(int(home.get("id")), int(away.get("id")), kickoff)
    h2h_edge = h2h_signal(int(home.get("id")), int(away.get("id")))
    injuries_edge = injuries_signal(api_fixture_id, int(home.get("id")), int(away.get("id")))
    home_form = parse_form(payload)["record"]
    away_form = parse_form(away_payload)["record"]
    home_news = (payload.get("overview", {}).get("newsSummary") or {}).get("items", [])
    away_news = (away_payload.get("overview", {}).get("newsSummary") or {}).get("items", [])
    news_count = len(home_news) + len(away_news)

    prediction, confidence = classify_prediction(
        home_points,
        away_points,
        stats["home_rank"],
        stats["away_rank"],
    )
    home_attack_edge, away_attack_edge = attack_defense_edge(
        stats["home_goals_per_match"],
        stats["away_goals_per_match"],
        stats["home_conceded_per_match"],
        stats["away_conceded_per_match"],
    )
    prediction, confidence = improve_prediction(
        prediction,
        home_points,
        away_points,
        home_history_score,
        away_history_score,
        stats["home_rank"],
        stats["away_rank"],
        home_attack_edge + h2h_edge + injuries_edge,
        away_attack_edge - h2h_edge - injuries_edge,
    )
    sureness_percent = estimate_sureness_percent(
        home_points,
        away_points,
        home_history_score,
        away_history_score,
        stats["home_rank"],
        stats["away_rank"],
        prediction,
    )
    sureness_percent -= prediction_contradiction_penalty(
        prediction,
        home_points,
        away_points,
        home_history_score,
        away_history_score,
        stats["home_rank"],
        stats["away_rank"],
        home_attack_edge + h2h_edge + injuries_edge,
        away_attack_edge - h2h_edge - injuries_edge,
    )
    sureness_percent += int(abs(h2h_edge) * 10) + int(abs(injuries_edge) * 8)
    sureness_percent = max(50, min(90, sureness_percent))

    interest = league_priority(next_match.get("tournament", {}).get("leagueId", league_id))
    if stats["home_rank"] and stats["away_rank"]:
        interest += max(0, 18 - int(stats["home_rank"]) - int(stats["away_rank"]))
    interest += max(home_points, away_points)
    interest += max(home_attack_edge, away_attack_edge) * 4
    interest += sureness_percent / 2

    return InterestingMatch(
        match_id=int(next_match["id"]),
        league_id=next_match.get("tournament", {}).get("leagueId", league_id),
        league_name=league_name,
        tournament_name=next_match.get("tournament", {}).get("name", league_name),
        kickoff_utc=kickoff,
        page_url=FOTMOB_BASE + next_match.get("pageUrl", ""),
        home_name=home.get("name", ""),
        away_name=away.get("name", ""),
        home_id=int(home.get("id", 0)),
        away_id=int(away.get("id", 0)),
        home_form=home_form,
        away_form=away_form,
        home_points=home_points,
        away_points=away_points,
        home_history_score=home_history_score,
        away_history_score=away_history_score,
        h2h_edge=h2h_edge,
        injuries_edge=injuries_edge,
        home_rank=stats["home_rank"],
        away_rank=stats["away_rank"],
        home_goals_per_match=stats["home_goals_per_match"],
        away_goals_per_match=stats["away_goals_per_match"],
        home_conceded_per_match=stats["home_conceded_per_match"],
        away_conceded_per_match=stats["away_conceded_per_match"],
        home_key_player=team_key_player(payload),
        away_key_player=team_key_player(away_payload),
        prediction=prediction,
        confidence=confidence,
        sureness_percent=sureness_percent,
        consensus_notes=build_consensus_notes(
            home_points,
            away_points,
            home_history_score,
            away_history_score,
            h2h_edge,
            injuries_edge,
            stats["home_rank"],
            stats["away_rank"],
            news_count,
            prediction,
        ),
        interest_score=float(interest),
        why=explain_interest(
            league_name,
            stats["home_rank"],
            stats["away_rank"],
            home_points,
            away_points,
            next_match.get("tournament", {}).get("name", league_name),
        ),
    )


def collect_interesting_matches(limit: int = 3, min_percent: int = 70) -> list[InterestingMatch]:
    candidates: dict[int, InterestingMatch] = {}
    for league_id in DEFAULT_LEAGUES:
        try:
            top_teams = top_teams_for_league(league_id)
        except requests.RequestException:
            continue
        league_name = ""
        if top_teams:
            league_name = top_teams[0].get("leagueName", "")
        for row in top_teams:
            try:
                item = build_candidate_from_team(int(row["id"]), league_name or str(league_id), league_id)
            except requests.RequestException:
                continue
            if not item:
                continue
            if item.tournament_name not in POPULAR_LEAGUE_NAMES and item.league_name not in POPULAR_LEAGUE_NAMES:
                continue
            if item.sureness_percent < min_percent:
                continue
            if item.prediction == "12":
                continue
            if item.confidence == "prudente" and item.sureness_percent < min_percent + 4:
                continue
            old = candidates.get(item.match_id)
            if old is None or item.interest_score > old.interest_score:
                candidates[item.match_id] = item

    matches = list(candidates.values())
    matches.sort(key=lambda item: (-item.interest_score, item.kickoff_utc))
    return matches[:limit]


def format_match_block(index: int, match: InterestingMatch) -> str:
    kickoff = datetime.fromisoformat(match.kickoff_utc.replace("Z", "+00:00")).strftime("%d/%m %H:%M UTC")
    rank_line = ""
    if match.home_rank and match.away_rank:
        rank_line = f"{match.home_rank}e vs {match.away_rank}e"
    gpm_line = ""
    if match.home_goals_per_match is not None and match.away_goals_per_match is not None:
        gpm_line = f"{match.home_goals_per_match:.2f} / {match.away_goals_per_match:.2f} buts par match"

    why = ", ".join(match.why)
    consensus = ", ".join(match.consensus_notes)
    confidence_badge = {
        "forte": "A",
        "moyenne": "B",
        "prudente": "C",
    }.get(match.confidence, "C")
    return "\n".join(
        [
            f"<b>{index}. {html.escape(match.home_name)} vs {html.escape(match.away_name)}</b>",
            f"{html.escape(match.tournament_name)} | {kickoff}",
            f"Pick: <b>{html.escape(match.prediction)}</b>  Confiance: <b>{confidence_badge}</b>  Taux: <b>{match.sureness_percent}%</b>",
            f"Forme: {html.escape(match.home_form)} | {html.escape(match.away_form)}",
            f"Historique 3 ans: {match.home_name} {match.home_history_score:.2f} | {match.away_name} {match.away_history_score:.2f}",
            f"H2H / Absences: {match.h2h_edge:+.2f} | {match.injuries_edge:+.2f}",
            f"Classement: {html.escape(rank_line or 'indisponible')}",
            f"Focus: {html.escape(match.home_key_player)} | {html.escape(match.away_key_player)}",
            f"Stats: {html.escape(gpm_line or 'stats limitees')}",
            f"Consensus: {html.escape(consensus or 'signaux croises limites')}",
            f"Interet: {html.escape(why)}",
            f"<a href=\"{html.escape(match.page_url)}\">Ouvrir le match</a>",
        ]
    )


def format_notification(matches: list[InterestingMatch], min_percent: int) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")
    blocks = [
        "<b>GABFOOT | Matchs Surs</b>",
        f"Mise a jour: {now}",
        f"Selection auto des matchs les plus surs des ligues populaires. Seuil: {min_percent}%",
        "",
    ]
    for idx, match in enumerate(matches, start=1):
        blocks.append(format_match_block(idx, match))
        blocks.append("")
    blocks.append("Frequence: toutes les 3 heures")
    return "\n".join(blocks).strip()


def send_telegram_html(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant dans .env")
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


def run_once(limit: int, force: bool, min_percent: int) -> bool:
    matches = collect_interesting_matches(limit=limit, min_percent=min_percent)
    if not matches:
        return False
    message = format_notification(matches, min_percent)
    signature = f"{min_percent}|" + "|".join(f"{item.match_id}:{item.sureness_percent}" for item in matches)
    state = load_state()
    if not force and state.get("last_sent_signature") == signature:
        return False
    send_telegram_html(message)
    state["last_sent_signature"] = signature
    state["history"] = [
        {
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "signature": signature,
        }
    ] + state.get("history", [])[:20]
    save_state(state)
    save_data_cache(DATA_CACHE)
    return True


def main() -> int:
    load_dotenv(ENV_FILE)
    parser = argparse.ArgumentParser(description="Envoie les matchs les plus interessants sur Telegram.")
    parser.add_argument("--limit", type=int, default=3, help="Nombre de matchs a envoyer")
    parser.add_argument("--every-hours", type=int, default=3, help="Frequence en heures")
    parser.add_argument("--min-percent", type=int, default=70, help="Seuil minimal de sureness")
    parser.add_argument("--loop", action="store_true", help="Boucle infinie")
    parser.add_argument("--force", action="store_true", help="Envoi meme si la signature n'a pas change")
    args = parser.parse_args()

    if args.loop:
        while True:
            try:
                sent = run_once(limit=args.limit, force=args.force, min_percent=args.min_percent)
                print(f"[{datetime.now().isoformat()}] sent={sent}", flush=True)
            except Exception as exc:
                print(f"[{datetime.now().isoformat()}] error={exc}", flush=True)
            time.sleep(max(1, args.every_hours) * 3600)
    else:
        sent = run_once(limit=args.limit, force=args.force, min_percent=args.min_percent)
        print("sent" if sent else "skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
