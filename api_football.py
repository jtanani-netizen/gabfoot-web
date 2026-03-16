from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple
import os

import requests
from dateutil import parser as dateparser

BASE_URL = "https://v3.football.api-sports.io"


@dataclass
class Event:
    id: int
    home: str
    away: str
    home_id: int
    away_id: int
    home_score: int
    away_score: int
    date: datetime
    tournament: str
    status: str
    home_logo: str = ""
    away_logo: str = ""
    league_logo: str = ""


class ApiFootballError(Exception):
    pass


def _get(path: str, params: dict) -> List[dict]:
    key = os.getenv("API_FOOTBALL_KEY")
    if not key:
        raise ApiFootballError("Définis API_FOOTBALL_KEY dans ton .env pour appeler l'API.")

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        "x-apisports-key": key,
    }

    resp = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("response", [])


def search_team(name: str) -> Tuple[int, str]:
    data = _get("/teams", {"search": name})
    if not data:
        raise ApiFootballError(f"Aucune équipe trouvée pour: {name}")
    team_info = data[0]["team"]
    return team_info["id"], team_info["name"]


def _normalize_fixture(item: dict) -> Event:
    fixture = item.get("fixture", {})
    teams = item.get("teams", {})
    goals = item.get("goals", {})
    league = item.get("league", {}).get("name", "")
    league_logo = item.get("league", {}).get("logo", "")
    date_str = fixture.get("date")
    date = dateparser.parse(date_str) if date_str else datetime.utcnow()
    status = fixture.get("status", {}).get("long", "")
    home = teams.get("home", {})
    away = teams.get("away", {})
    return Event(
        id=fixture.get("id", 0),
        home=home.get("name", ""),
        away=away.get("name", ""),
        home_id=home.get("id", 0),
        away_id=away.get("id", 0),
        home_score=goals.get("home") if goals.get("home") is not None else 0,
        away_score=goals.get("away") if goals.get("away") is not None else 0,
        date=date,
        tournament=league,
        status=status,
        home_logo=home.get("logo", ""),
        away_logo=away.get("logo", ""),
        league_logo=league_logo,
    )


def last_events(team_id: int, limit: int = 5) -> List[Event]:
    data = _get("/fixtures", {"team": team_id, "last": limit})
    return [_normalize_fixture(entry) for entry in data]


def head_to_head(team_a: int, team_b: int, limit: int = 5) -> List[Event]:
    data = _get("/fixtures", {"h2h": f"{team_a}-{team_b}", "last": limit})
    return [_normalize_fixture(entry) for entry in data][:limit]


def fixtures_next(league_id: int, count: int = 3) -> List[Event]:
    """Prochains matchs d'une ligue (triés par date)."""
    data = _get("/fixtures", {"league": league_id, "season": datetime.utcnow().year, "next": count})
    events = [_normalize_fixture(entry) for entry in data]
    return sorted(events, key=lambda e: e.date)[:count]
