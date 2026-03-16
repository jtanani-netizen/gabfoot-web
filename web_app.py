#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from analyze import get_json, parse_form
from notify_interesting_matches import ENV_FILE, InterestingMatch, collect_interesting_matches, top_teams_for_league
from send_demo_model_style import generate_card
from send_safe_matches_image import CARDS_DIR, send_photo, to_card_match


HOST = "127.0.0.1"
PORT = 8012
CARD_PATH = CARDS_DIR / "web_safe_matches_card.png"
ICON_PATH = CARDS_DIR / "gabfoot_icon.png"
BOTOLA_LEAGUE_ID = 530
TEAM_CACHE: dict[int, dict] = {}
APP_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
DASHBOARD_CACHE_FILE = APP_CACHE_DIR / "dashboard_cache.json"
DASHBOARD_CACHE_TTL_SECONDS = 15 * 60
DASHBOARD_CACHE_STALE_SECONDS = 6 * 3600
_DASHBOARD_CACHE: dict[str, dict[str, object]] = {}
_CACHE_LOCK = threading.Lock()
_REFRESH_IN_FLIGHT: set[str] = set()
ARTICLE_IMAGE_CACHE: dict[str, str] = {}
ARTICLE_IMAGE_LOCK = threading.Lock()


def dashboard_cache_key(limit: int, min_percent: int) -> str:
    return f"{limit}:{min_percent}"


def serialize_match(match: InterestingMatch) -> dict[str, object]:
    return {
        "match_id": match.match_id,
        "league_id": match.league_id,
        "league_name": match.league_name,
        "tournament_name": match.tournament_name,
        "kickoff_utc": match.kickoff_utc,
        "page_url": match.page_url,
        "home_name": match.home_name,
        "away_name": match.away_name,
        "home_id": match.home_id,
        "away_id": match.away_id,
        "home_form": match.home_form,
        "away_form": match.away_form,
        "home_points": match.home_points,
        "away_points": match.away_points,
        "home_history_score": match.home_history_score,
        "away_history_score": match.away_history_score,
        "h2h_edge": match.h2h_edge,
        "injuries_edge": match.injuries_edge,
        "home_rank": match.home_rank,
        "away_rank": match.away_rank,
        "home_goals_per_match": match.home_goals_per_match,
        "away_goals_per_match": match.away_goals_per_match,
        "home_conceded_per_match": match.home_conceded_per_match,
        "away_conceded_per_match": match.away_conceded_per_match,
        "home_key_player": match.home_key_player,
        "away_key_player": match.away_key_player,
        "prediction": match.prediction,
        "confidence": match.confidence,
        "sureness_percent": match.sureness_percent,
        "consensus_notes": match.consensus_notes,
        "interest_score": match.interest_score,
        "why": match.why,
    }


def deserialize_match(payload: dict[str, object]) -> InterestingMatch:
    return InterestingMatch(**payload)


def load_dashboard_cache() -> None:
    if not DASHBOARD_CACHE_FILE.exists():
        return
    try:
        payload = json.loads(DASHBOARD_CACHE_FILE.read_text())
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    for key, entry in payload.items():
        if isinstance(entry, dict):
            _DASHBOARD_CACHE[key] = entry


def persist_dashboard_cache() -> None:
    APP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_CACHE_FILE.write_text(json.dumps(_DASHBOARD_CACHE, ensure_ascii=True, indent=2))


def compute_dashboard_payload(limit: int, min_percent: int) -> dict[str, object]:
    matches, card_path = build_card(limit=limit, min_percent=min_percent)
    botola = safe_botola_predictions()
    tennis = safe_tennis_world_matches()
    articles = safe_football_articles(matches, botola)
    return {
        "updated_at": time.time(),
        "limit": limit,
        "min_percent": min_percent,
        "has_card": bool(card_path and card_path.exists()),
        "matches": [serialize_match(match) for match in matches],
        "botola": botola,
        "tennis": tennis,
        "articles": articles,
    }


def refresh_dashboard_cache(limit: int, min_percent: int) -> None:
    key = dashboard_cache_key(limit, min_percent)
    try:
        payload = compute_dashboard_payload(limit=limit, min_percent=min_percent)
        with _CACHE_LOCK:
            _DASHBOARD_CACHE[key] = payload
            persist_dashboard_cache()
    finally:
        with _CACHE_LOCK:
            _REFRESH_IN_FLIGHT.discard(key)


def ensure_dashboard_refresh(limit: int, min_percent: int) -> None:
    key = dashboard_cache_key(limit, min_percent)
    with _CACHE_LOCK:
        if key in _REFRESH_IN_FLIGHT:
            return
        _REFRESH_IN_FLIGHT.add(key)
    thread = threading.Thread(target=refresh_dashboard_cache, args=(limit, min_percent), daemon=True)
    thread.start()


def get_cached_dashboard_payload(limit: int, min_percent: int) -> tuple[dict[str, object] | None, bool]:
    key = dashboard_cache_key(limit, min_percent)
    now = time.time()
    with _CACHE_LOCK:
        entry = _DASHBOARD_CACHE.get(key)
    if entry:
        age = now - float(entry.get("updated_at", 0))
        if age <= DASHBOARD_CACHE_TTL_SECONDS:
            return entry, False
        if age <= DASHBOARD_CACHE_STALE_SECONDS:
            ensure_dashboard_refresh(limit=limit, min_percent=min_percent)
            return entry, True
    ensure_dashboard_refresh(limit=limit, min_percent=min_percent)
    return None, True


def get_fresh_dashboard_payload(limit: int, min_percent: int) -> dict[str, object]:
    payload = compute_dashboard_payload(limit=limit, min_percent=min_percent)
    key = dashboard_cache_key(limit, min_percent)
    with _CACHE_LOCK:
        _DASHBOARD_CACHE[key] = payload
        persist_dashboard_cache()
    return payload


def get_team_payload(team_id: int) -> dict:
    if team_id not in TEAM_CACHE:
        TEAM_CACHE[team_id] = get_json(f"https://www.fotmob.com/api/teams?id={team_id}", timeout=35)
    return TEAM_CACHE[team_id]


def find_meta_image(page_html: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def resolve_article_image(article_url: str, fallback: str = "") -> str:
    article_url = article_url.strip()
    if not article_url:
        return fallback

    with ARTICLE_IMAGE_LOCK:
        if article_url in ARTICLE_IMAGE_CACHE:
            cached = ARTICLE_IMAGE_CACHE[article_url]
            return cached or fallback

    image_url = ""
    try:
        request = Request(
            article_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
        )
        with urlopen(request, timeout=12) as response:
            payload = response.read(300_000).decode("utf-8", "ignore")
        image_url = find_meta_image(payload)
    except (TimeoutError, URLError, ValueError, OSError):
        image_url = ""

    with ARTICLE_IMAGE_LOCK:
        ARTICLE_IMAGE_CACHE[article_url] = image_url
    return image_url or fallback


def form_points(record: str) -> int:
    score = 0
    for part in record.split():
        if part.endswith("G"):
            score += int(part[:-1]) * 3
        elif part.endswith("N"):
            score += int(part[:-1])
    return score


def botola_predictions(limit: int = 6) -> list[dict[str, object]]:
    payload = get_json(f"https://www.fotmob.com/api/leagues?id={BOTOLA_LEAGUE_ID}", timeout=40)
    table_rows = (payload.get("table") or [{}])[0].get("data", {}).get("table", {}).get("all", [])
    standings = {int(row["id"]): row for row in table_rows if row.get("id")}
    all_matches = payload.get("fixtures", {}).get("allMatches", [])
    pending = [match for match in all_matches if not match.get("status", {}).get("finished")]
    pending.sort(key=lambda item: item.get("status", {}).get("utcTime", ""))

    out: list[dict[str, object]] = []
    for match in pending[:limit]:
        home = match.get("home", {})
        away = match.get("away", {})
        home_id = int(home.get("id", 0))
        away_id = int(away.get("id", 0))
        if not home_id or not away_id:
            continue

        home_payload = get_team_payload(home_id)
        away_payload = get_team_payload(away_id)
        home_record = parse_form(home_payload)["record"]
        away_record = parse_form(away_payload)["record"]
        home_row = standings.get(home_id, {})
        away_row = standings.get(away_id, {})

        home_score = (
            form_points(home_record)
            + int(home_row.get("pts", 0))
            + int(home_row.get("goalConDiff", 0))
            + 2
        )
        away_score = (
            form_points(away_record)
            + int(away_row.get("pts", 0))
            + int(away_row.get("goalConDiff", 0))
        )
        delta = home_score - away_score
        if delta >= 6:
            prediction = "1"
        elif delta <= -6:
            prediction = "2"
        else:
            prediction = "X"
        confidence = max(54, min(84, 56 + abs(delta) * 2))

        why = []
        if home_row.get("idx") and away_row.get("idx"):
            why.append(f"Classement {home_row.get('idx')}e vs {away_row.get('idx')}e")
        if form_points(home_record) != form_points(away_record):
            side = home.get("shortName") if form_points(home_record) > form_points(away_record) else away.get("shortName")
            why.append(f"Forme recente pour {side}")
        if int(home_row.get("goalConDiff", 0)) != int(away_row.get("goalConDiff", 0)):
            side = home.get("shortName") if int(home_row.get("goalConDiff", 0)) > int(away_row.get("goalConDiff", 0)) else away.get("shortName")
            why.append(f"Difference de buts favorable a {side}")

        out.append(
            {
                "kickoff": fmt_kickoff(match.get("status", {}).get("utcTime", "")),
                "home": home.get("name", ""),
                "away": away.get("name", ""),
                "homeId": home_id,
                "awayId": away_id,
                "prediction": prediction,
                "confidence": confidence,
                "homeRecord": home_record,
                "awayRecord": away_record,
                "homeRank": home_row.get("idx"),
                "awayRank": away_row.get("idx"),
                "why": why[:3],
                "status": match.get("status", {}).get("reason", {}).get("short", ""),
            }
        )
    return out


def tennis_world_matches(limit: int = 8) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for circuit in ("atp", "wta"):
        payload = get_json(f"https://site.api.espn.com/apis/site/v2/sports/tennis/{circuit}/scoreboard", timeout=40)
        for event in payload.get("events", [])[:2]:
            tournament = event.get("name") or event.get("shortName") or circuit.upper()
            for grouping in event.get("groupings", []):
                for competition in grouping.get("competitions", [])[:4]:
                    status = competition.get("status", {}).get("type", {})
                    competitors = competition.get("competitors", [])
                    if len(competitors) < 2:
                        continue
                    players = sorted(competitors, key=lambda item: item.get("order", 99))
                    p1 = players[0].get("athlete", {}).get("displayName", "Joueur 1")
                    p2 = players[1].get("athlete", {}).get("displayName", "Joueur 2")
                    lines1 = "-".join(str(int(s.get("value", 0))) for s in players[0].get("linescores", []) if s.get("value") is not None) or "-"
                    lines2 = "-".join(str(int(s.get("value", 0))) for s in players[1].get("linescores", []) if s.get("value") is not None) or "-"
                    venue = competition.get("venue", {})
                    out.append(
                        {
                            "tour": circuit.upper(),
                            "tournament": tournament,
                            "group": grouping.get("grouping", {}).get("displayName", ""),
                            "player1": p1,
                            "player2": p2,
                            "score": f"{lines1} / {lines2}",
                            "status": status.get("shortDetail") or status.get("description") or "",
                            "state": status.get("state", ""),
                            "venue": venue.get("fullName", ""),
                            "court": venue.get("court", ""),
                        }
                    )
                    if len(out) >= limit:
                        return out
    return out[:limit]


def safe_botola_predictions(limit: int = 6) -> list[dict[str, object]]:
    try:
        return botola_predictions(limit=limit)
    except Exception:
        return []


def safe_tennis_world_matches(limit: int = 8) -> list[dict[str, object]]:
    try:
        return tennis_world_matches(limit=limit)
    except Exception:
        return []


def football_articles(matches: list[InterestingMatch], botola: list[dict[str, object]], limit: int = 12) -> list[dict[str, str]]:
    seen_team_ids: list[int] = []
    for match in matches[:4]:
        for team_id in (match.home_id, match.away_id):
            if team_id not in seen_team_ids:
                seen_team_ids.append(team_id)
    for match in botola[:4]:
        for team_id in (int(match.get("homeId", 0)), int(match.get("awayId", 0))):
            if team_id and team_id not in seen_team_ids:
                seen_team_ids.append(team_id)

    seen_articles: set[str] = set()
    articles: list[dict[str, str]] = []
    for team_id in seen_team_ids:
        payload = get_team_payload(team_id)
        team_name = payload.get("details", {}).get("name", "")
        team_logo = (
            payload.get("sportsTeamJSONLD", {}).get("logo")
            or payload.get("details", {}).get("logo")
            or ""
        )
        items = (payload.get("overview", {}).get("newsSummary") or {}).get("items", [])
        for item in items[:4]:
            source = item.get("source", {}) or {}
            url = str(source.get("uri", "")).strip()
            title = str(source.get("title", "")).strip()
            summary = str(item.get("summary", "")).strip()
            source_name = str(source.get("sourceName", "Source")).strip()
            key = url or title
            if not key or key in seen_articles:
                continue
            seen_articles.add(key)
            article_image = resolve_article_image(url, fallback=str(team_logo).strip())
            articles.append(
                {
                    "team": team_name,
                    "title": title or "Article football",
                    "summary": summary or "Resume indisponible.",
                    "url": url,
                    "source": source_name,
                    "image": article_image,
                    "logo": str(team_logo).strip(),
                }
            )
            if len(articles) >= limit:
                return articles
    return articles


def safe_football_articles(matches: list[InterestingMatch], botola: list[dict[str, object]], limit: int = 12) -> list[dict[str, str]]:
    try:
        return football_articles(matches, botola, limit=limit)
    except Exception:
        return []


def interaction_script() -> str:
    return """
<script>
(() => {
  let audioCtx = null;
  let lastTapAt = 0;

  function playSoftTap() {
    const now = Date.now();
    if (now - lastTapAt < 70) return;
    lastTapAt = now;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const ctx = audioCtx;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(720, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(520, ctx.currentTime + 0.05);
      gain.gain.setValueAtTime(0.0001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.018, ctx.currentTime + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.06);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.065);
    } catch (err) {
      // Ignore browsers that block audio before first interaction.
    }
  }

  document.addEventListener('pointerdown', (event) => {
    const target = event.target.closest('a, button, .nav-link, .ghost-btn, .btn');
    if (!target) return;
    playSoftTap();
  }, { passive: true });
})();
</script>
"""


def render_pick_badge(prediction: str, percent: int) -> str:
    return f"""
    <div class="pick-badge">
      <span class="pick-value">{html.escape(prediction)}</span>
      <span class="pick-percent">{percent}%</span>
    </div>
    """


def render_favorite_button(
    match_id: int,
    title: str,
    kickoff: str,
    prediction: str,
    percent: int,
    detail_url: str,
) -> str:
    return f"""
    <button
      class="favorite-toggle"
      type="button"
      data-match-id="{match_id}"
      data-match-title="{html.escape(title)}"
      data-match-kickoff="{html.escape(kickoff)}"
      data-match-pick="{html.escape(prediction)}"
      data-match-percent="{percent}"
      data-match-url="{html.escape(detail_url)}"
      aria-label="Ajouter aux favoris"
    >☆</button>
    """


def pick_theme(prediction: str) -> str:
    normalized = prediction.strip().upper()
    if "1" in normalized and "2" not in normalized and "X" not in normalized:
        return "pick-home"
    if "2" in normalized and "1" not in normalized and "X" not in normalized:
        return "pick-away"
    if "X" in normalized and "1" not in normalized and "2" not in normalized:
        return "pick-draw"
    return "pick-mixed"


def confidence_theme(percent: int) -> str:
    if percent >= 85:
        return "confidence-elite"
    if percent >= 75:
        return "confidence-strong"
    return "confidence-medium"


def render_confidence_meter(percent: int) -> str:
    width = max(10, min(100, percent))
    return f"""
    <div class="confidence-meter {confidence_theme(percent)}">
      <span style="width:{width}%"></span>
    </div>
    """


def fmt_kickoff(kickoff_utc: str) -> str:
    dt = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%d/%m/%Y %H:%M")


def parse_kickoff(kickoff_utc: str) -> datetime:
    return datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00")).astimezone()


def build_card(limit: int, min_percent: int) -> tuple[list[InterestingMatch], Path | None]:
    matches = collect_interesting_matches(limit=limit, min_percent=min_percent)
    if not matches:
        return matches, None
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    batch = [to_card_match(match) for match in matches[:6]]
    generate_card(batch, title=f"Matchs surs {min_percent}%+", out_path=str(CARD_PATH))
    return matches, CARD_PATH


def render_prediction_table(matches: list[InterestingMatch]) -> str:
    grouped_rows: dict[str, list[str]] = {}
    ordered_slots: list[str] = []

    for match in sorted(matches, key=lambda item: parse_kickoff(item.kickoff_utc)):
        kickoff = parse_kickoff(match.kickoff_utc)
        kickoff_label = kickoff.strftime("%d/%m/%Y %H:%M")
        slot = kickoff.strftime("%d/%m/%Y • %H:00")
        why = " • ".join(match.why[:2]) or "Analyse disponible"
        consensus = " / ".join(match.consensus_notes[:2]) or "Consensus stable"
        pick_class = pick_theme(match.prediction)
        confidence_class = confidence_theme(match.sureness_percent)
        favorite_button = render_favorite_button(
            match.match_id,
            f"{match.home_name} vs {match.away_name}",
            kickoff_label,
            match.prediction,
            match.sureness_percent,
            f"/match/{match.match_id}",
        )
        row = (
            f"""
            <tr class="prediction-row reveal {pick_class} {confidence_class}">
              <td>
                <div class="row-kickoff-time">{kickoff.strftime("%H:%M")}</div>
                <div class="row-kickoff-date">{kickoff.strftime("%d/%m/%Y")}</div>
              </td>
              <td>
                <div class="row-league">{html.escape(match.tournament_name)}</div>
                <div class="row-match">{html.escape(match.home_name)} <span>vs</span> {html.escape(match.away_name)}</div>
                <div class="row-why">{html.escape(why)}</div>
                <div class="row-actions"><a class="row-link" href="/match/{match.match_id}">Voir l'analyse</a>{favorite_button}</div>
              </td>
              <td>
                <div class="table-pick {pick_class}">{html.escape(match.prediction)}</div>
                <div class="table-percent">{match.sureness_percent}% fiable</div>
                {render_confidence_meter(match.sureness_percent)}
              </td>
              <td>
                <div class="row-form">{html.escape(match.home_form)} | {html.escape(match.away_form)}</div>
                <div class="row-meta">Classement {match.home_rank or '-'} / {match.away_rank or '-'}</div>
              </td>
              <td>
                <div class="row-meta">Hist. {match.home_history_score:.2f} / {match.away_history_score:.2f}</div>
                <div class="row-meta">H2H {match.h2h_edge:+.2f} | Abs. {match.injuries_edge:+.2f}</div>
              </td>
              <td>
                <div class="row-meta">{html.escape(match.home_key_player)} | {html.escape(match.away_key_player)}</div>
                <div class="row-meta">{html.escape(consensus)}</div>
              </td>
              <td>
                <div class="row-meta">{html.escape(' • '.join(match.why))}</div>
                <div class="row-meta">Buts {match.home_goals_per_match:.2f} / {match.away_goals_per_match:.2f}</div>
                <div class="row-meta">Def. {match.home_conceded_per_match:.2f} / {match.away_conceded_per_match:.2f}</div>
              </td>
            </tr>
            """
        )
        if slot not in grouped_rows:
            grouped_rows[slot] = []
            ordered_slots.append(slot)
        grouped_rows[slot].append(row)

    if not ordered_slots:
        return '<div class="footer">Aucun match ne passe le filtre actuel.</div>'

    bodies = []
    for slot in ordered_slots:
        bodies.append(
            f"""
            <tbody>
              <tr class="hour-separator">
                <td colspan="7">Heure des matchs : {html.escape(slot)}</td>
              </tr>
              {''.join(grouped_rows[slot])}
            </tbody>
            """
        )
    return f"""
    <div class="table-shell">
      <table class="prediction-table">
        <thead>
          <tr>
            <th>Heure</th>
            <th>Match</th>
            <th>Pick</th>
            <th>Forme</th>
            <th>Stats</th>
            <th>Joueurs / Consensus</th>
            <th>Details match</th>
          </tr>
        </thead>
        {''.join(bodies)}
      </table>
    </div>
    """


def render_botola_table(botola: list[dict[str, object]]) -> str:
    grouped_rows: dict[str, list[str]] = {}
    ordered_slots: list[str] = []
    for match in botola:
        kickoff_text = str(match["kickoff"])
        slot = kickoff_text[:13].replace(" ", " • ") if len(kickoff_text) >= 13 else kickoff_text
        prediction = str(match["prediction"])
        confidence = int(match["confidence"])
        pick_class = pick_theme(prediction)
        confidence_class = confidence_theme(confidence)
        row = (
            f"""
            <tr class="prediction-row reveal {pick_class} {confidence_class}">
              <td>
                <div class="row-kickoff-time">{html.escape(kickoff_text[-5:] if len(kickoff_text) >= 5 else kickoff_text)}</div>
                <div class="row-kickoff-date">{html.escape(kickoff_text[:-6] if len(kickoff_text) > 6 else kickoff_text)}</div>
              </td>
              <td>
                <div class="row-league">Botola Pro</div>
                <div class="row-match">{html.escape(str(match['home']))} <span>vs</span> {html.escape(str(match['away']))}</div>
                <div class="row-why">{html.escape(' • '.join(match['why']))}</div>
              </td>
              <td>
                <div class="table-pick {pick_class}">{html.escape(prediction)}</div>
                <div class="table-percent">{confidence}% confiance</div>
                {render_confidence_meter(confidence)}
              </td>
              <td>
                <div class="row-form">{html.escape(str(match['homeRecord']))} | {html.escape(str(match['awayRecord']))}</div>
                <div class="row-meta">Rang {match['homeRank'] or '-'} / {match['awayRank'] or '-'}</div>
                <div class="row-meta">Etat {html.escape(str(match['status'])) or 'A venir'}</div>
              </td>
            </tr>
            """
        )
        if slot not in grouped_rows:
            grouped_rows[slot] = []
            ordered_slots.append(slot)
        grouped_rows[slot].append(row)
    if not ordered_slots:
        return '<div class="footer">Aucun match marocain exploitable pour le moment.</div>'
    bodies = []
    for slot in ordered_slots:
        bodies.append(
            f"""
            <tbody>
              <tr class="hour-separator">
                <td colspan="4">Heure des matchs : {html.escape(slot)}</td>
              </tr>
              {''.join(grouped_rows[slot])}
            </tbody>
            """
        )
    return f"""
    <div class="table-shell compact-table-shell">
      <table class="prediction-table compact-table">
        <thead>
          <tr>
            <th>Horaire</th>
            <th>Match</th>
            <th>Pronostic</th>
            <th>Lecture rapide</th>
          </tr>
        </thead>
        {''.join(bodies)}
      </table>
    </div>
    """


def render_tennis_table(tennis: list[dict[str, object]]) -> str:
    grouped_rows: dict[str, list[str]] = {}
    ordered_slots: list[str] = []
    for item in tennis:
        status = str(item.get("status", "")).strip()
        slot = status[:16] if status else str(item.get("tournament", "ATP/WTA"))
        row = (
            f"""
            <tr class="prediction-row reveal pick-mixed confidence-medium">
              <td>
                <div class="row-kickoff-time">{html.escape(str(item['tour']))}</div>
                <div class="row-kickoff-date">{html.escape(str(item['group']))}</div>
              </td>
              <td>
                <div class="row-league">{html.escape(str(item['tournament']))}</div>
                <div class="row-match">{html.escape(str(item['player1']))} <span>vs</span> {html.escape(str(item['player2']))}</div>
              </td>
              <td>
                <div class="table-pick pick-mixed">{html.escape(str(item['score'])[:1] if str(item['score']) else '-')}</div>
                <div class="table-percent">{html.escape(status or 'En cours')}</div>
                {render_confidence_meter(68)}
              </td>
              <td>
                <div class="row-meta">Score {html.escape(str(item['score']))}</div>
                <div class="row-meta">Lieu {html.escape(str(item['venue']))}{' | ' + html.escape(str(item['court'])) if item['court'] else ''}</div>
              </td>
            </tr>
            """
        )
        if slot not in grouped_rows:
            grouped_rows[slot] = []
            ordered_slots.append(slot)
        grouped_rows[slot].append(row)
    if not ordered_slots:
        return '<div class="footer">Aucun match tennis disponible actuellement.</div>'
    bodies = []
    for slot in ordered_slots:
        bodies.append(
            f"""
            <tbody>
              <tr class="hour-separator">
                <td colspan="4">Heure / statut : {html.escape(slot)}</td>
              </tr>
              {''.join(grouped_rows[slot])}
            </tbody>
            """
        )
    return f"""
    <div class="table-shell compact-table-shell">
      <table class="prediction-table compact-table">
        <thead>
          <tr>
            <th>Circuit</th>
            <th>Match</th>
            <th>Score / Etat</th>
            <th>Details</th>
          </tr>
        </thead>
        {''.join(bodies)}
      </table>
    </div>
    """


def render_articles_grid(articles: list[dict[str, str]]) -> str:
    cards = []
    for item in articles:
        image = str(item.get("image", "")).strip()
        visual = (
            f'<img class="article-image" src="{html.escape(image)}" alt="{html.escape(item["title"])}">'
            if image
            else '<div class="article-image article-image-fallback">GABFOOT</div>'
        )
        logo = str(item.get("logo", "")).strip()
        badge = (
            f'<img class="article-badge" src="{html.escape(logo)}" alt="{html.escape(item["team"])}">'
            if logo
            else '<div class="article-badge article-badge-fallback">GF</div>'
        )
        link = (
            f'<a class="article-link" href="{html.escape(item["url"])}" target="_blank" rel="noreferrer">Lire l&apos;article</a>'
            if item.get("url")
            else ""
        )
        cards.append(
            f"""
            <article class="article-showcase reveal">
              <div class="article-visual-wrap">
                {visual}
                <div class="article-overlay"></div>
                <div class="article-badge-wrap">{badge}</div>
              </div>
              <div class="article-copy">
                <div class="article-topline">Football News</div>
                <h3>{html.escape(item['title'])}</h3>
                <div class="kickoff">{html.escape(item['team'])} • {html.escape(item['source'])}</div>
                <p>{html.escape(item['summary'])}</p>
                {link}
              </div>
            </article>
            """
        )
    return "".join(cards) or '<div class="footer">Aucun article remonte pour le moment.</div>'


def render_market_strip(matches: list[InterestingMatch], botola: list[dict[str, object]]) -> str:
    pills = []
    for match in matches[:4]:
        pills.append(
            f"""
            <div class="market-pill {pick_theme(match.prediction)} {confidence_theme(match.sureness_percent)}">
              <span class="market-pill-label">{html.escape(match.home_name)} - {html.escape(match.away_name)}</span>
              <span class="market-pill-value">{html.escape(match.prediction)} • {match.sureness_percent}%</span>
            </div>
            """
        )
    for match in botola[:3]:
        prediction = str(match.get("prediction", ""))
        confidence = int(match.get("confidence", 0))
        pills.append(
            f"""
            <div class="market-pill {pick_theme(prediction)} {confidence_theme(confidence)}">
              <span class="market-pill-label">Botola • {html.escape(str(match.get("home", "")))} - {html.escape(str(match.get("away", "")))}</span>
              <span class="market-pill-value">{html.escape(prediction)} • {confidence}%</span>
            </div>
            """
        )
    return "".join(pills)


def favorites_script() -> str:
    return """
<script>
(() => {
  const KEY = 'gabfoot_favorites_v1';

  function escapeHtml(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function readFavorites() {
    try {
      return JSON.parse(localStorage.getItem(KEY) || '[]');
    } catch (err) {
      return [];
    }
  }

  function writeFavorites(items) {
    localStorage.setItem(KEY, JSON.stringify(items.slice(0, 24)));
  }

  function isFavorite(matchId) {
    return readFavorites().some((item) => String(item.id) === String(matchId));
  }

  function syncButtons() {
    document.querySelectorAll('.favorite-toggle').forEach((button) => {
      const active = isFavorite(button.dataset.matchId);
      button.classList.toggle('active', active);
      button.textContent = active ? '★' : '☆';
      button.setAttribute('aria-label', active ? 'Retirer des favoris' : 'Ajouter aux favoris');
    });
  }

  function renderFavoritesPanel() {
    const root = document.querySelector('#favorites-list');
    const empty = document.querySelector('#favorites-empty');
    if (!root || !empty) return;
    const items = readFavorites();
    root.innerHTML = items.map((item) => `
      <a class="favorite-card" href="${escapeHtml(item.url)}">
        <span class="favorite-card-title">${escapeHtml(item.title)}</span>
        <span class="favorite-card-meta">${escapeHtml(item.kickoff)}</span>
        <span class="favorite-card-pick">${escapeHtml(item.pick)} • ${escapeHtml(item.percent)}%</span>
      </a>
    `).join('');
    empty.style.display = items.length ? 'none' : 'block';
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('.favorite-toggle');
    if (!button) return;
    const items = readFavorites();
    const matchId = String(button.dataset.matchId);
    const index = items.findIndex((item) => String(item.id) === matchId);
    if (index >= 0) {
      items.splice(index, 1);
    } else {
      items.unshift({
        id: matchId,
        title: button.dataset.matchTitle || 'Match',
        kickoff: button.dataset.matchKickoff || '',
        pick: button.dataset.matchPick || '',
        percent: button.dataset.matchPercent || '',
        url: button.dataset.matchUrl || '/',
      });
    }
    writeFavorites(items);
    syncButtons();
    renderFavoritesPanel();
  });

  syncButtons();
  renderFavoritesPanel();
})();
</script>
"""


def find_match_for_detail(match_id: int, limit: int = 12, min_percent: int = 55) -> InterestingMatch | None:
    with _CACHE_LOCK:
        cached_entries = list(_DASHBOARD_CACHE.values())
    for entry in cached_entries:
        for item in entry.get("matches", []):
            if int(item.get("match_id", 0)) == match_id:
                return deserialize_match(item)
    try:
        matches = collect_interesting_matches(limit=limit, min_percent=min_percent)
    except Exception:
        return None
    for match in matches:
        if match.match_id == match_id:
            return match
    return None


def render_match_detail_page(match: InterestingMatch, articles: list[dict[str, str]]) -> str:
    kickoff = fmt_kickoff(match.kickoff_utc)
    pick_class = pick_theme(match.prediction)
    confidence_class = confidence_theme(match.sureness_percent)
    reasons = "".join(f"<li>{html.escape(item)}</li>" for item in match.why)
    consensus = "".join(f"<li>{html.escape(item)}</li>" for item in match.consensus_notes)
    article_cards = render_articles_grid(articles)
    favorite_button = render_favorite_button(
        match.match_id,
        f"{match.home_name} vs {match.away_name}",
        kickoff,
        match.prediction,
        match.sureness_percent,
        f"/match/{match.match_id}",
    )
    external_link = (
        f'<a class="btn" href="{html.escape(match.page_url)}" target="_blank" rel="noreferrer">Voir sur FotMob</a>'
        if match.page_url else ""
    )
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(match.home_name)} vs {html.escape(match.away_name)} | GABFOOT</title>
  <meta name="description" content="Analyse detaillee GABFOOT pour {html.escape(match.home_name)} vs {html.escape(match.away_name)}.">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:#06110d; --panel:rgba(8,18,18,.86); --line:rgba(103,255,182,.14); --text:#f4fff8;
      --muted:#97b2aa; --accent:#67ffb6; --accent2:#ffe066; --shadow:0 22px 60px rgba(0,0,0,.32);
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0; color:var(--text); font-family:"Space Grotesk","DejaVu Sans",sans-serif;
      background:
        radial-gradient(circle at top left, rgba(103,255,182,.16), transparent 24%),
        radial-gradient(circle at 85% 10%, rgba(255,224,102,.10), transparent 18%),
        linear-gradient(180deg, #04100d 0%, #071916 44%, #06110d 100%);
    }}
    .wrap {{ max-width:1200px; margin:0 auto; padding:24px 18px 52px; }}
    .hero, .panel {{
      border:1px solid var(--line); border-radius:28px; background:var(--panel); box-shadow:var(--shadow);
      backdrop-filter:blur(18px);
    }}
    .hero {{ padding:24px; }}
    .links {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; }}
    .links a, .btn {{
      display:inline-flex; align-items:center; min-height:44px; padding:0 16px; border-radius:999px; text-decoration:none;
      font-weight:800; border:1px solid var(--line); color:var(--text); background:rgba(255,255,255,.04);
    }}
    .btn {{ background:linear-gradient(90deg,var(--accent),var(--accent2)); color:#07120d; border:0; }}
    .favorite-toggle {{
      appearance:none; border:1px solid var(--line); cursor:pointer; display:inline-flex; align-items:center; justify-content:center;
      min-width:44px; min-height:44px; padding:0 14px; border-radius:999px; background:rgba(255,255,255,.04); color:var(--text);
      font-size:20px; font-weight:900; transition:transform .18s ease, background .18s ease, border-color .18s ease, color .18s ease;
    }}
    .favorite-toggle:hover, .favorite-toggle.active {{
      transform:translateY(-1px); border-color:rgba(255,224,102,.36); background:rgba(255,224,102,.12); color:var(--accent2);
    }}
    .hero-top {{ display:grid; gap:18px; grid-template-columns:1.1fr .9fr; align-items:start; }}
    .match-title {{ margin:14px 0 8px; font-size:40px; line-height:1.05; }}
    .match-title span {{ color:var(--muted); font-size:24px; }}
    .meta {{ color:var(--muted); font-size:14px; }}
    .detail-pick {{
      display:inline-flex; align-items:center; justify-content:center; min-width:88px; min-height:88px; border-radius:26px;
      font-size:34px; font-weight:900; color:#07120d; margin-top:18px;
      background:linear-gradient(135deg,#67ffb6,#ffe066);
    }}
    .detail-pick.pick-home {{ background:linear-gradient(135deg,#67ffb6,#b9ff7d); }}
    .detail-pick.pick-away {{ background:linear-gradient(135deg,#7db7ff,#9be7ff); }}
    .detail-pick.pick-draw {{ background:linear-gradient(135deg,#ffe066,#ffd18f); }}
    .detail-pick.pick-mixed {{ background:linear-gradient(135deg,#ffb37d,#ffe066); }}
    .hero-side {{
      display:grid; gap:12px;
    }}
    .metric-grid {{
      display:grid; gap:14px; grid-template-columns:repeat(2,minmax(0,1fr)); margin-top:18px;
    }}
    .metric, .side-card {{
      padding:16px; border-radius:20px; background:rgba(255,255,255,.04); border:1px solid rgba(103,255,182,.08);
    }}
    .metric-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; font-weight:700; }}
    .metric-value {{ margin-top:8px; font-size:22px; font-weight:800; }}
    .panel {{ margin-top:18px; padding:18px; }}
    .panel h2 {{ margin:0 0 12px; font-size:24px; }}
    .bullet-list {{ margin:0; padding-left:18px; color:var(--muted); line-height:1.65; }}
    .articles-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; }}
    .kick-meter {{ margin-top:12px; width:120px; height:8px; border-radius:999px; background:rgba(255,255,255,.08); overflow:hidden; }}
    .kick-meter span {{ display:block; height:100%; width:{match.sureness_percent}%; background:linear-gradient(90deg,#67ffb6,#ffe066); }}
    @media (max-width: 980px) {{
      .hero-top {{ grid-template-columns:1fr; }}
      .metric-grid, .articles-grid {{ grid-template-columns:1fr; }}
      .match-title {{ font-size:30px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
        <div class="links">
          <a href="/">Accueil</a>
          <a href="/botola">Botola Pro</a>
          <a href="/articles">Articles</a>
          {external_link}
          {favorite_button}
        </div>
      <div class="hero-top">
        <div>
          <div class="meta"><a href="/league/{match.league_id}" style="color:#97b2aa;text-decoration:none;">{html.escape(match.tournament_name)}</a> • {html.escape(kickoff)}</div>
          <h1 class="match-title"><a href="/team/{match.home_id}" style="color:#f4fff8;text-decoration:none;">{html.escape(match.home_name)}</a> <span>vs</span> <a href="/team/{match.away_id}" style="color:#f4fff8;text-decoration:none;">{html.escape(match.away_name)}</a></h1>
          <div class="detail-pick {pick_class}">{html.escape(match.prediction)}</div>
          <div class="meta" style="margin-top:12px;">Confiance GABFOOT : {match.sureness_percent}%</div>
          <div class="kick-meter"><span></span></div>
        </div>
        <div class="hero-side">
          <div class="side-card">
            <div class="metric-label">Forme recente</div>
            <div class="metric-value">{html.escape(match.home_form)} | {html.escape(match.away_form)}</div>
          </div>
          <div class="side-card">
            <div class="metric-label">Joueurs cles</div>
            <div class="metric-value" style="font-size:18px;">{html.escape(match.home_key_player)} | {html.escape(match.away_key_player)}</div>
          </div>
          <div class="side-card">
            <div class="metric-label">Consensus</div>
            <div class="metric-value" style="font-size:18px;">{' / '.join(html.escape(item) for item in match.consensus_notes) or 'Aucun signal fort'}</div>
          </div>
        </div>
      </div>
      <div class="metric-grid">
        <div class="metric">
          <div class="metric-label">Classement</div>
          <div class="metric-value">{match.home_rank or '-'} / {match.away_rank or '-'}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Historique 3 ans</div>
          <div class="metric-value">{match.home_history_score:.2f} / {match.away_history_score:.2f}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Buts par match</div>
          <div class="metric-value">{match.home_goals_per_match:.2f} / {match.away_goals_per_match:.2f}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Buts encaisses</div>
          <div class="metric-value">{match.home_conceded_per_match:.2f} / {match.away_conceded_per_match:.2f}</div>
        </div>
        <div class="metric">
          <div class="metric-label">H2H</div>
          <div class="metric-value">{match.h2h_edge:+.2f}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Absences</div>
          <div class="metric-value">{match.injuries_edge:+.2f}</div>
        </div>
      </div>
    </section>

    <section class="panel">
      <h2>Pourquoi ce pick</h2>
      <ul class="bullet-list">{reasons}</ul>
    </section>

    <section class="panel">
      <h2>Lecture GABFOOT</h2>
      <ul class="bullet-list">{consensus}</ul>
    </section>

    <section class="panel">
      <h2>Articles lies</h2>
      <div class="articles-grid">
        {article_cards}
      </div>
    </section>
  </div>
</body>
{interaction_script()}
{favorites_script()}
</html>"""


def render_team_detail_page(team_id: int) -> str:
    payload = get_team_payload(team_id)
    details = payload.get("details", {}) or {}
    overview = payload.get("overview", {}) or {}
    top_players = overview.get("topPlayers") or {}
    next_match = overview.get("nextMatch") or {}

    def first_player(bucket: str) -> str:
        bucket_payload = top_players.get(bucket) or {}
        players = bucket_payload.get("players") or []
        if not players:
            return "N/A"
        player = players[0]
        return f"{player.get('name', 'Joueur')} ({player.get('value', '-')})"

    logo = (
        payload.get("sportsTeamJSONLD", {}).get("logo")
        or details.get("logo")
        or ""
    )
    visual = f'<img src="{html.escape(str(logo))}" alt="{html.escape(details.get("name", "Equipe"))}" style="width:140px;height:140px;object-fit:contain;border-radius:28px;background:rgba(255,255,255,.05);padding:16px;border:1px solid rgba(255,255,255,.08);">' if logo else ""
    form_record = parse_form(payload).get("record", "N/A")
    venue = (overview.get("venue") or {}).get("name") or "Stade indisponible"
    next_match_name = ""
    if next_match:
        home = (next_match.get("home") or {}).get("name", "")
        away = (next_match.get("away") or {}).get("name", "")
        if home or away:
            next_match_name = f"{home} vs {away}".strip()
    related_articles = []
    for item in ((overview.get("newsSummary") or {}).get("items") or [])[:6]:
        source = item.get("source") or {}
        related_articles.append(
            {
                "team": str(details.get("name", "Equipe")),
                "title": str(source.get("title", "Article club")),
                "summary": str(item.get("summary", "Resume indisponible.")),
                "url": str(source.get("uri", "")),
                "source": str(source.get("sourceName", "Source")),
                "image": str(logo),
                "logo": str(logo),
            }
        )
    article_cards = render_articles_grid(related_articles)
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(details.get("name", "Equipe"))} | GABFOOT</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700;800&display=swap" rel="stylesheet">
  <style>
    body {{ margin:0; font-family:"Space Grotesk","DejaVu Sans",sans-serif; color:#f4fff8; background:linear-gradient(180deg,#04100d 0%,#071916 44%,#06110d 100%); }}
    .wrap {{ max-width:1180px; margin:0 auto; padding:24px 18px 52px; }}
    .hero,.panel {{ border:1px solid rgba(103,255,182,.12); border-radius:28px; background:rgba(8,18,18,.84); box-shadow:0 22px 60px rgba(0,0,0,.32); }}
    .hero {{ padding:24px; display:grid; gap:18px; grid-template-columns:160px 1fr; align-items:center; }}
    .links {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:10px; }}
    .links a {{ color:#f4fff8; text-decoration:none; border:1px solid rgba(103,255,182,.14); border-radius:999px; padding:10px 14px; background:rgba(255,255,255,.04); }}
    h1 {{ margin:8px 0; font-size:42px; color:#67ffb6; }}
    .meta {{ color:#97b2aa; line-height:1.6; }}
    .grid {{ display:grid; gap:18px; grid-template-columns:repeat(4,minmax(0,1fr)); margin-top:18px; }}
    .card {{ padding:16px; border-radius:20px; background:rgba(255,255,255,.04); border:1px solid rgba(103,255,182,.08); }}
    .label {{ color:#97b2aa; font-size:12px; text-transform:uppercase; letter-spacing:.08em; font-weight:700; }}
    .value {{ margin-top:8px; font-size:20px; font-weight:800; }}
    .panel {{ margin-top:18px; padding:18px; }}
    .panel h2 {{ margin:0 0 12px; font-size:24px; }}
    .articles-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; }}
    @media (max-width:980px) {{ .hero,.grid,.articles-grid {{ grid-template-columns:1fr; }} h1 {{ font-size:32px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div>{visual}</div>
      <div>
        <div class="links">
          <a href="/">Accueil</a>
          <a href="/league/{details.get('primaryLeagueId', 0)}">Ligue</a>
        </div>
        <h1>{html.escape(details.get("name", "Equipe"))}</h1>
        <div class="meta">{html.escape(details.get("country", {}).get("name", "") if isinstance(details.get("country"), dict) else str(details.get("country", "")))} • {html.escape(str(details.get("primaryLeagueName", "Ligue principale")))} • {html.escape(venue)}</div>
      </div>
    </section>
    <section class="panel">
      <h2>Vue rapide</h2>
      <div class="grid">
        <div class="card"><div class="label">Forme</div><div class="value">{html.escape(form_record)}</div></div>
        <div class="card"><div class="label">Buteur</div><div class="value">{html.escape(first_player('byGoals'))}</div></div>
        <div class="card"><div class="label">Meilleure note</div><div class="value">{html.escape(first_player('byRating'))}</div></div>
        <div class="card"><div class="label">Passe decisive</div><div class="value">{html.escape(first_player('byAssists'))}</div></div>
      </div>
    </section>
    <section class="panel">
      <h2>Prochain match</h2>
      <div class="meta">{html.escape(next_match_name or 'Prochain match indisponible')}</div>
    </section>
    <section class="panel">
      <h2>Articles lies</h2>
      <div class="articles-grid">{article_cards}</div>
    </section>
  </div>
</body>
{interaction_script()}
</html>"""


def render_league_detail_page(league_id: int) -> str:
    payload = get_json(f"https://www.fotmob.com/api/leagues?id={league_id}", timeout=40)
    details = payload.get("details", {}) or {}
    rows = top_teams_for_league(league_id, limit=10)
    fixtures = (payload.get("fixtures") or {}).get("allMatches") or []
    upcoming = [item for item in fixtures if not item.get("status", {}).get("finished")]
    upcoming.sort(key=lambda item: item.get("status", {}).get("utcTime", ""))
    table_rows = []
    for row in rows:
        team_id = int(row.get("id", 0))
        link = f"/team/{team_id}" if team_id else "#"
        table_rows.append(
            f"""
            <tr>
              <td>{row.get('idx', '-')}</td>
              <td><a href="{link}">{html.escape(str(row.get('name', 'Equipe')))}</a></td>
              <td>{row.get('played', '-')}</td>
              <td>{row.get('wins', '-')}</td>
              <td>{row.get('draws', '-')}</td>
              <td>{row.get('losses', '-')}</td>
              <td>{html.escape(str(row.get('scoresStr', '-')))}</td>
              <td>{row.get('pts', '-')}</td>
            </tr>
            """
        )
    fixture_cards = []
    for item in upcoming[:8]:
        fixture_cards.append(
            f"""
            <div class="fixture-card">
              <div class="fixture-time">{html.escape(fmt_kickoff(item.get('status', {}).get('utcTime', '')))}</div>
              <div class="fixture-match">{html.escape((item.get('home') or {}).get('name', ''))} vs {html.escape((item.get('away') or {}).get('name', ''))}</div>
            </div>
            """
        )
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(str(details.get('name', 'Ligue')))} | GABFOOT</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700;800&display=swap" rel="stylesheet">
  <style>
    body {{ margin:0; font-family:"Space Grotesk","DejaVu Sans",sans-serif; color:#f4fff8; background:linear-gradient(180deg,#04100d 0%,#071916 44%,#06110d 100%); }}
    .wrap {{ max-width:1180px; margin:0 auto; padding:24px 18px 52px; }}
    .hero,.panel {{ border:1px solid rgba(103,255,182,.12); border-radius:28px; background:rgba(8,18,18,.84); box-shadow:0 22px 60px rgba(0,0,0,.32); }}
    .hero,.panel {{ padding:24px; }}
    .links {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:10px; }}
    .links a {{ color:#f4fff8; text-decoration:none; border:1px solid rgba(103,255,182,.14); border-radius:999px; padding:10px 14px; background:rgba(255,255,255,.04); }}
    h1 {{ margin:8px 0; font-size:42px; color:#67ffb6; }}
    .meta {{ color:#97b2aa; line-height:1.6; }}
    table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:14px 12px; text-align:left; border-bottom:1px solid rgba(255,255,255,.06); }}
    th {{ color:#97b2aa; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    td a {{ color:#f4fff8; text-decoration:none; font-weight:700; }}
    .fixtures {{ display:grid; gap:12px; grid-template-columns:repeat(2,minmax(0,1fr)); }}
    .fixture-card {{ padding:16px; border-radius:20px; background:rgba(255,255,255,.04); border:1px solid rgba(103,255,182,.08); }}
    .fixture-time {{ color:#67ffb6; font-size:13px; font-weight:800; }}
    .fixture-match {{ margin-top:8px; font-size:18px; font-weight:700; }}
    @media (max-width:980px) {{ .fixtures {{ grid-template-columns:1fr; }} h1 {{ font-size:32px; }} .panel {{ overflow-x:auto; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="links">
        <a href="/">Accueil</a>
      </div>
      <h1>{html.escape(str(details.get('name', 'Ligue')))}</h1>
      <div class="meta">Classement, equipes leaders et prochains matchs sur la meme page.</div>
    </section>
    <section class="panel">
      <h2 style="margin-top:0;">Classement</h2>
      <table>
        <thead>
          <tr><th>#</th><th>Equipe</th><th>J</th><th>G</th><th>N</th><th>P</th><th>Buts</th><th>Pts</th></tr>
        </thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table>
    </section>
    <section class="panel">
      <h2 style="margin-top:0;">Prochains matchs</h2>
      <div class="fixtures">{''.join(fixture_cards) or '<div class="meta">Aucun match a venir.</div>'}</div>
    </section>
  </div>
</body>
{interaction_script()}
</html>"""


def page_html(
    matches: list[InterestingMatch],
    card_path: Path | None,
    limit: int,
    min_percent: int,
    notice: str = "",
    botola: list[dict[str, object]] | None = None,
    tennis: list[dict[str, object]] | None = None,
    articles: list[dict[str, str]] | None = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    botola = botola or []
    tennis = tennis or []
    articles = articles or []
    featured_match = matches[0] if matches else None
    featured_article = articles[0] if articles else None
    avg_confidence = int(round(sum(match.sureness_percent for match in matches) / len(matches))) if matches else 0
    prediction_table = render_prediction_table(matches)
    botola_table = render_botola_table(botola)
    tennis_table = render_tennis_table(tennis)
    article_cards = render_articles_grid(articles)
    market_strip = render_market_strip(matches, botola)

    image_block = ""
    if card_path and card_path.exists():
        image_block = """
        <section class="panel image-panel reveal">
          <div class="panel-head">
            <h2>Affiche actuelle</h2>
            <a class="ghost-btn" href="/image" target="_blank">Ouvrir l'image</a>
          </div>
          <img class="poster" src="/image" alt="Affiche matchs surs">
        </section>
        """
    notice_html = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
    lead_panel = ""
    if featured_match:
        featured_favorite = render_favorite_button(
            featured_match.match_id,
            f"{featured_match.home_name} vs {featured_match.away_name}",
            fmt_kickoff(featured_match.kickoff_utc),
            featured_match.prediction,
            featured_match.sureness_percent,
            f"/match/{featured_match.match_id}",
        )
        lead_panel = f"""
        <section class="spotlight-card reveal">
          <div class="spotlight-label">Focus premium</div>
          <h2>{html.escape(featured_match.home_name)} <span>vs</span> {html.escape(featured_match.away_name)}</h2>
          <div class="spotlight-meta">{html.escape(featured_match.tournament_name)} • {html.escape(fmt_kickoff(featured_match.kickoff_utc))}</div>
          <div class="spotlight-badges">
            <span>{html.escape(featured_match.prediction)} • {featured_match.sureness_percent}%</span>
            <span>{html.escape(featured_match.home_form)} | {html.escape(featured_match.away_form)}</span>
          </div>
          <p>{html.escape(' • '.join(featured_match.why))}</p>
          <div style="margin-top:18px; display:flex; gap:10px; flex-wrap:wrap;"><a class="btn" href="/match/{featured_match.match_id}">Voir la fiche match</a>{featured_favorite}</div>
        </section>
        """
    editorial_panel = ""
    if featured_article:
        image = str(featured_article.get("image", "")).strip()
        visual = f'<img class="editorial-image" src="{html.escape(image)}" alt="{html.escape(featured_article["team"])}">' if image else '<div class="editorial-image editorial-fallback">NEWS</div>'
        article_link = f'<a class="btn" href="{html.escape(featured_article["url"])}" target="_blank" rel="noreferrer">Lire l&apos;analyse</a>' if featured_article.get("url") else ""
        editorial_panel = f"""
        <section class="editorial-card reveal">
          <div class="editorial-visual">{visual}</div>
          <div class="editorial-copy">
            <div class="spotlight-label">Article vedette</div>
            <h3>{html.escape(featured_article["title"])}</h3>
            <div class="kickoff">{html.escape(featured_article["team"])} • {html.escape(featured_article["source"])}</div>
            <p>{html.escape(featured_article["summary"])}</p>
            {article_link}
          </div>
        </section>
        """
    favorites_panel = """
    <section id="favorites-zone" class="panel reveal section-anchor" style="grid-column: 1 / -1;">
      <div class="panel-head">
        <div class="panel-title">
          <span class="panel-pill">Mon espace</span>
          <h2>Mes favoris</h2>
        </div>
        <span class="footer">Matchs enregistres sur cet appareil</span>
      </div>
      <div class="favorites-board">
        <div id="favorites-list" class="favorites-grid"></div>
        <div id="favorites-empty" class="favorites-empty">Ajoute des matchs depuis les tableaux ou les fiches pour construire ton board personnel.</div>
      </div>
    </section>
    """
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GABFOOT | Pronostics Foot Premium</title>
  <meta name="description" content="GABFOOT centralise les meilleurs pronostics, les matchs classes par heure, la Botola Pro, le tennis et les articles foot dans un seul dashboard premium.">
  <meta property="og:title" content="GABFOOT | Pronostics Foot Premium">
  <meta property="og:description" content="Pronostics classes par heure, analyses premium, articles foot et dashboard GABFOOT.">
  <meta property="og:type" content="website">
  <meta property="og:image" content="/icon.png">
  <meta name="theme-color" content="#08150c">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="icon" type="image/png" href="/icon.png">
  <link rel="apple-touch-icon" href="/icon.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #06110d;
      --panel: rgba(8, 18, 18, 0.84);
      --panel-2: rgba(12, 28, 24, 0.95);
      --line: rgba(103, 255, 182, 0.12);
      --line-strong: rgba(103, 255, 182, 0.32);
      --text: #f4fff8;
      --muted: #97b2aa;
      --accent: #67ffb6;
      --accent-2: #ffe066;
      --accent-3: #7db7ff;
      --danger: #ffd18f;
      --shadow: 0 22px 60px rgba(0,0,0,.32);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Space Grotesk", "DejaVu Sans", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(103,255,182,0.17), transparent 24%),
        radial-gradient(circle at 85% 10%, rgba(255,224,102,0.11), transparent 18%),
        radial-gradient(circle at bottom right, rgba(125,183,255,0.12), transparent 22%),
        linear-gradient(180deg, #04100d 0%, #071916 44%, #06110d 100%);
      min-height: 100vh;
    }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 24px 18px 52px; }}
    html {{ scroll-behavior: smooth; }}
    .hero {{
      position: relative;
      overflow: hidden;
      padding: 24px;
      border: 1px solid var(--line-strong);
      border-radius: 30px;
      background:
        linear-gradient(135deg, rgba(16, 38, 24, 0.94), rgba(9, 18, 14, 0.88)),
        linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.01));
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }}
    .hero::after {{
      content: "";
      position: absolute;
      inset: auto -10% -120px auto;
      width: 260px;
      height: 260px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(103,255,182,.18), transparent 62%);
      animation: pulseOrbit 5s ease-in-out infinite;
      pointer-events: none;
    }}
    .hero::before {{
      content: "";
      position: absolute;
      inset: 0;
      background:
        radial-gradient(circle at 20% 0%, rgba(103,255,182,.16), transparent 28%),
        radial-gradient(circle at 100% 10%, rgba(125,183,255,.10), transparent 22%);
      pointer-events: none;
    }}
    .nav-links {{
      position: relative;
      z-index: 1;
      display:flex; flex-wrap:wrap; gap:10px; margin-top:18px;
    }}
    .nav-link {{
      display:inline-flex; align-items:center; min-height:42px; padding:0 15px;
      border-radius:999px; color:var(--text); text-decoration:none; border:1px solid var(--line);
      background: rgba(255,255,255,.04); font-size:14px; font-weight:700;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
      transition: transform .18s ease, border-color .18s ease, background .18s ease;
    }}
    .nav-link:hover, .nav-link:active {{
      transform: translateY(-1px);
      border-color: var(--line-strong);
      background: rgba(124,255,107,.10);
    }}
    .hero-stats {{
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .hero-stat {{
      position: relative;
      overflow: hidden;
      padding: 14px 16px;
      border-radius: 20px;
      border: 1px solid rgba(103,255,182,.14);
      background: linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.03));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
    }}
    .hero-stat::before {{
      content: "";
      position: absolute;
      inset: auto -10px -40px auto;
      width: 88px;
      height: 88px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(103,255,182,.16), transparent 66%);
    }}
    .hero-stat-label {{
      position: relative;
      z-index: 1;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-weight: 700;
    }}
    .hero-stat-value {{
      position: relative;
      z-index: 1;
      margin-top: 8px;
      font-size: 28px;
      font-weight: 800;
      line-height: 1;
    }}
    .hero-stat-note {{
      position: relative;
      z-index: 1;
      margin-top: 6px;
      color: #d8eee6;
      font-size: 13px;
    }}
    .market-strip {{
      position: relative;
      z-index: 1;
      display: flex;
      gap: 10px;
      overflow-x: auto;
      margin-top: 18px;
      padding-bottom: 4px;
      scrollbar-width: none;
    }}
    .market-strip::-webkit-scrollbar {{ display: none; }}
    .market-pill {{
      min-width: 220px;
      padding: 12px 14px;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.04);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
    }}
    .market-pill-label {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-weight: 700;
    }}
    .market-pill-value {{
      display: block;
      margin-top: 8px;
      font-size: 18px;
      font-weight: 800;
    }}
    h1 {{ margin: 0; font-size: 44px; letter-spacing: .03em; color: var(--accent); position: relative; z-index: 1; }}
    .sub {{ margin-top: 8px; color: var(--muted); font-size: 16px; position: relative; z-index: 1; max-width: 760px; line-height: 1.5; }}
    .hero-strip {{ position: relative; z-index: 1; display:grid; gap:16px; margin-top:22px; grid-template-columns: 1.08fr .92fr; }}
    .spotlight-card, .editorial-card {{
      position: relative; overflow:hidden; border-radius: 28px; padding: 22px; min-height: 250px;
      border: 1px solid rgba(124,255,107,.18); box-shadow: var(--shadow);
      background: linear-gradient(135deg, rgba(8,34,30,.95), rgba(6,14,12,.94));
    }}
    .spotlight-card::before, .editorial-card::before {{
      content:""; position:absolute; inset:0;
      background: radial-gradient(circle at top right, rgba(255,224,102,.16), transparent 24%),
                  radial-gradient(circle at bottom left, rgba(103,255,182,.14), transparent 28%);
      pointer-events:none;
    }}
    .editorial-card {{ display:grid; grid-template-columns: 180px 1fr; gap:18px; align-items:center; }}
    .spotlight-label {{
      display:inline-flex; padding:7px 11px; border-radius:999px; font-size:12px; font-weight:800;
      color:#07120d; background:linear-gradient(90deg, var(--accent), var(--accent-2));
    }}
    .spotlight-card h2, .editorial-copy h3 {{ margin:16px 0 8px; font-size:34px; line-height:1.05; }}
    .spotlight-card h2 span {{ color: var(--muted); font-size:24px; }}
    .spotlight-meta {{ color: var(--muted); font-size:14px; }}
    .spotlight-badges {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }}
    .spotlight-badges span {{
      padding:10px 12px; border-radius:14px; border:1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.05); color: var(--text); font-size:13px; font-weight:700;
    }}
    .spotlight-card p, .editorial-copy p {{ margin:16px 0 0; color: var(--muted); line-height:1.55; font-size:15px; }}
    .editorial-image {{
      width: 180px; height: 180px; object-fit: cover; border-radius: 26px; background: rgba(255,255,255,.06);
      border:1px solid rgba(255,255,255,.10);
    }}
    .editorial-fallback, .article-image-fallback, .article-badge-fallback {{
      display:flex; align-items:center; justify-content:center; color:#d8ff7a; font-weight:900; letter-spacing:.14em;
    }}
    .toolbar {{
      display: flex; flex-wrap: wrap; gap: 12px; align-items: end;
      margin-top: 20px; position: relative; z-index: 1;
    }}
    .field {{ display: flex; flex-direction: column; gap: 6px; }}
    label {{ font-size: 13px; color: var(--muted); }}
    input {{
      width: 124px; padding: 11px 13px; border-radius: 14px;
      border: 1px solid var(--line); background: rgba(5,12,8,.72); color: var(--text);
      font-size: 15px; outline: none;
      transition: border-color .18s ease, box-shadow .18s ease;
    }}
    input:focus {{
      border-color: var(--line-strong);
      box-shadow: 0 0 0 3px rgba(124,255,107,.08);
    }}
    .btn, .ghost-btn {{
      appearance: none; border: 0; cursor: pointer; text-decoration: none;
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 46px; padding: 0 18px; border-radius: 14px; font-weight: 800;
      transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease, background .18s ease;
    }}
    .btn {{
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
      color: #10230d;
      box-shadow: 0 12px 30px rgba(124,255,107,.18);
    }}
    .ghost-btn {{ border: 1px solid var(--line); color: var(--text); background: rgba(255,255,255,.05); }}
    .btn:hover, .ghost-btn:hover, .btn:active, .ghost-btn:active {{
      transform: translateY(-1px) scale(1.01);
      box-shadow: 0 14px 34px rgba(0,0,0,.26);
    }}
    .notice {{
      margin-top: 16px; padding: 12px 14px; border-radius: 12px;
      background: rgba(255, 207, 128, 0.1); color: var(--danger); border: 1px solid rgba(255, 207, 128, 0.18);
    }}
    .grid {{ display: grid; gap: 18px; margin-top: 22px; grid-template-columns: 1.12fr .88fr; }}
    .panel {{
      border: 1px solid var(--line); border-radius: 24px; background: var(--panel); padding: 18px;
      box-shadow: var(--shadow); backdrop-filter: blur(16px);
    }}
    .section-anchor {{
      scroll-margin-top: 24px;
    }}
    .panel-head {{ display:flex; align-items:center; justify-content:space-between; gap: 12px; margin-bottom: 14px; }}
    .panel-title {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .panel-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 12px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .08em;
      background: rgba(103,255,182,.12);
      border: 1px solid rgba(103,255,182,.14);
      color: var(--accent);
    }}
    .panel h2 {{ margin: 0; font-size: 22px; letter-spacing: .02em; }}
    .league {{ color: var(--accent); font-size: 14px; font-weight: 700; }}
    .kickoff {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
    .table-shell {{
      overflow: hidden;
      border-radius: 20px;
      border: 1px solid rgba(103,255,182,.14);
      background: linear-gradient(180deg, rgba(12,29,24,.96), rgba(8,17,16,.98));
    }}
    .prediction-table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 980px;
    }}
    .prediction-table thead th {{
      padding: 16px 18px;
      text-align: left;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--muted);
      background: rgba(255,255,255,.03);
      border-bottom: 1px solid rgba(103,255,182,.12);
    }}
    .hour-separator td {{
      padding: 12px 18px;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--accent-2);
      background: linear-gradient(90deg, rgba(103,255,182,.12), rgba(255,224,102,.08));
      border-bottom: 1px solid rgba(103,255,182,.12);
    }}
    .prediction-row {{
      transition: transform .22s ease, background .22s ease;
    }}
    .prediction-row.pick-home td:first-child,
    .market-pill.pick-home,
    .table-pick.pick-home {{ box-shadow: inset 4px 0 0 rgba(103,255,182,.65); }}
    .prediction-row.pick-away td:first-child,
    .market-pill.pick-away,
    .table-pick.pick-away {{ box-shadow: inset 4px 0 0 rgba(125,183,255,.65); }}
    .prediction-row.pick-draw td:first-child,
    .market-pill.pick-draw,
    .table-pick.pick-draw {{ box-shadow: inset 4px 0 0 rgba(255,224,102,.65); }}
    .prediction-row.pick-mixed td:first-child,
    .market-pill.pick-mixed,
    .table-pick.pick-mixed {{ box-shadow: inset 4px 0 0 rgba(255,140,128,.55); }}
    .prediction-row:hover {{
      transform: translateY(-1px) scale(1.003);
      background: rgba(255,255,255,.02);
    }}
    .prediction-table td {{
      padding: 18px;
      vertical-align: top;
      border-bottom: 1px solid rgba(255,255,255,.05);
    }}
    .prediction-table tbody tr:last-child td {{ border-bottom: 0; }}
    .row-league {{ color: var(--accent); font-size: 13px; font-weight: 700; }}
    .row-kickoff, .row-meta {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 6px;
      line-height: 1.45;
    }}
    .row-kickoff-time {{
      font-size: 24px;
      font-weight: 800;
      color: var(--accent);
      line-height: 1;
    }}
    .row-kickoff-date {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .row-match {{
      font-size: 19px;
      font-weight: 700;
      line-height: 1.3;
    }}
    .row-match span {{ color: var(--muted); font-size: 14px; font-weight: 500; }}
    .row-why {{
      margin-top: 8px;
      color: #cfe5dd;
      font-size: 13px;
      line-height: 1.45;
    }}
    .row-actions {{ margin-top: 12px; }}
    .row-actions {{
      display:flex;
      align-items:center;
      gap:10px;
      flex-wrap:wrap;
    }}
    .row-link {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 999px;
      text-decoration: none;
      font-size: 12px;
      font-weight: 800;
      color: var(--text);
      border: 1px solid rgba(103,255,182,.16);
      background: rgba(255,255,255,.04);
    }}
    .row-link:hover {{ background: rgba(103,255,182,.10); }}
    .favorite-toggle {{
      appearance:none;
      border:1px solid rgba(255,224,102,.18);
      cursor:pointer;
      display:inline-flex;
      align-items:center;
      justify-content:center;
      min-width:38px;
      min-height:38px;
      padding:0 12px;
      border-radius:999px;
      background:rgba(255,255,255,.04);
      color:#fff0a8;
      font-size:18px;
      font-weight:900;
      line-height:1;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
      transition: transform .18s ease, border-color .18s ease, background .18s ease, color .18s ease;
    }}
    .favorite-toggle:hover, .favorite-toggle.active {{
      transform: translateY(-1px);
      border-color: rgba(255,224,102,.42);
      background: rgba(255,224,102,.12);
      color: var(--accent-2);
    }}
    .row-form {{
      color: var(--text);
      font-size: 14px;
      font-weight: 700;
    }}
    .table-pick {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 54px;
      min-height: 54px;
      padding: 0 14px;
      border-radius: 18px;
      font-size: 24px;
      font-weight: 800;
      color: #07120d;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      box-shadow: 0 14px 26px rgba(103,255,182,.16);
      animation: glowPulse 3.6s ease-in-out infinite;
    }}
    .table-pick.pick-home {{ background: linear-gradient(135deg, #67ffb6, #b9ff7d); }}
    .table-pick.pick-away {{ background: linear-gradient(135deg, #7db7ff, #9be7ff); }}
    .table-pick.pick-draw {{ background: linear-gradient(135deg, #ffe066, #ffd18f); }}
    .table-pick.pick-mixed {{ background: linear-gradient(135deg, #ffb37d, #ffe066); }}
    .table-percent {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .06em;
    }}
    .confidence-meter {{
      margin-top: 10px;
      width: 88px;
      height: 6px;
      border-radius: 999px;
      background: rgba(255,255,255,.08);
      overflow: hidden;
    }}
    .confidence-meter span {{
      display: block;
      height: 100%;
      border-radius: 999px;
    }}
    .confidence-meter.confidence-elite span {{ background: linear-gradient(90deg, #67ffb6, #d9ff7f); }}
    .confidence-meter.confidence-strong span {{ background: linear-gradient(90deg, #7db7ff, #67ffb6); }}
    .confidence-meter.confidence-medium span {{ background: linear-gradient(90deg, #ffd18f, #ffe066); }}
    .poster {{
      display: block; width: 100%; border-radius: 18px; border: 1px solid #2d4a34; background: #061008;
    }}
    .section-grid {{ display:grid; gap:18px; margin-top:18px; grid-template-columns: 1fr 1fr; }}
    .compact-table {{ min-width: 720px; }}
    .compact-table-shell {{ height: 100%; }}
    .match-list {{ display: grid; gap: 14px; }}
    .match-card {{
      padding: 17px; border-radius: 22px; background: var(--panel-2); border: 1px solid rgba(103,255,182,.10);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.03);
      transition: transform .28s ease, border-color .28s ease, background .28s ease;
    }}
    .match-card:hover {{
      transform: translateY(-4px) scale(1.01);
      border-color: rgba(103,255,182,.18);
      background: rgba(20, 35, 22, 0.98);
    }}
    .meta-row {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; }}
    .pick-badge {{
      min-width: 92px; border-radius: 18px; background: linear-gradient(180deg, rgba(19,38,23,.95), rgba(13,27,17,.95)); padding: 8px 10px;
      border: 1px solid rgba(103,255,182,.22); text-align: center;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.05);
    }}
    .pick-value {{ display:block; font-size: 24px; font-weight: 800; color: var(--accent); }}
    .pick-percent {{ display:block; font-size: 13px; color: var(--muted); }}
    .teams {{ margin-top: 12px; font-size: 24px; font-weight: 700; }}
    .teams span {{ color: var(--muted); font-size: 16px; font-weight: 500; }}
    .stats-grid {{
      margin-top: 14px; display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 10px;
    }}
    .stats-grid div {{
      padding: 11px 12px; border-radius: 16px; background: rgba(255,255,255,.035); border: 1px solid rgba(140,255,114,.08);
    }}
    .stats-grid strong {{ display:block; color: var(--accent-2); font-size: 12px; margin-bottom: 5px; }}
    .stats-grid span {{ display:block; color: var(--text); font-size: 14px; line-height: 1.35; }}
    .why {{ margin-top: 12px; font-size: 14px; color: var(--muted); }}
    .tennis-state {{
      min-width: 110px; padding: 8px 10px; border-radius: 14px; text-align:center;
      color: var(--accent-2); background: #132617; border: 1px solid #2f5b39; font-size: 13px; font-weight: 700;
    }}
    .articles-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 18px;
    }}
    .article-showcase {{
      position: relative;
      overflow: hidden;
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(10,26,22,.98), rgba(8,14,14,.98));
      border: 1px solid rgba(103,255,182,.12);
      box-shadow: var(--shadow);
      transition: transform .25s ease, border-color .25s ease;
    }}
    .article-showcase:hover {{
      transform: translateY(-4px);
      border-color: rgba(103,255,182,.24);
    }}
    .article-visual-wrap {{
      position: relative;
      height: 214px;
      overflow: hidden;
      background: rgba(255,255,255,.04);
    }}
    .article-image {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .article-overlay {{
      position: absolute;
      inset: 0;
      background: linear-gradient(180deg, transparent 10%, rgba(4,12,10,.18) 50%, rgba(4,12,10,.75) 100%);
    }}
    .article-badge-wrap {{
      position: absolute;
      left: 16px;
      bottom: 16px;
      z-index: 1;
    }}
    .article-badge {{
      width: 54px;
      height: 54px;
      object-fit: contain;
      border-radius: 16px;
      padding: 8px;
      background: rgba(8,18,18,.86);
      border: 1px solid rgba(255,255,255,.12);
      backdrop-filter: blur(12px);
    }}
    .article-copy {{
      padding: 18px;
    }}
    .article-topline {{
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    .article-copy h3 {{
      margin: 10px 0 8px;
      font-size: 22px;
      line-height: 1.25;
    }}
    .article-copy p {{
      margin: 12px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.55;
    }}
    .article-link {{
      display:inline-flex; margin-top:14px; color: var(--accent); text-decoration:none; font-weight:800;
    }}
    .favorites-board {{
      border-radius: 22px;
      border: 1px solid rgba(103,255,182,.10);
      background: linear-gradient(180deg, rgba(10,25,22,.92), rgba(7,15,14,.96));
      padding: 18px;
    }}
    .favorites-grid {{
      display:grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}
    .favorite-card {{
      display:flex;
      flex-direction:column;
      gap:8px;
      min-height:140px;
      padding:18px;
      border-radius:20px;
      border:1px solid rgba(255,224,102,.16);
      text-decoration:none;
      color:var(--text);
      background:
        radial-gradient(circle at top right, rgba(255,224,102,.10), transparent 28%),
        linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02));
      transition: transform .2s ease, border-color .2s ease, background .2s ease;
    }}
    .favorite-card:hover {{
      transform: translateY(-3px);
      border-color: rgba(255,224,102,.32);
      background:
        radial-gradient(circle at top right, rgba(255,224,102,.14), transparent 32%),
        linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.03));
    }}
    .favorite-card-title {{
      font-size:18px;
      font-weight:800;
      line-height:1.3;
    }}
    .favorite-card-meta {{
      color:var(--muted);
      font-size:13px;
      line-height:1.4;
    }}
    .favorite-card-pick {{
      margin-top:auto;
      display:inline-flex;
      align-items:center;
      width:max-content;
      min-height:34px;
      padding:0 12px;
      border-radius:999px;
      background:rgba(103,255,182,.10);
      color:var(--accent);
      font-size:13px;
      font-weight:800;
      border:1px solid rgba(103,255,182,.14);
    }}
    .favorites-empty {{
      padding: 20px;
      border-radius: 18px;
      border: 1px dashed rgba(255,255,255,.10);
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
      background: rgba(255,255,255,.02);
    }}
    .bottom-dock {{
      position: sticky;
      bottom: 16px;
      z-index: 20;
      display: none;
      gap: 10px;
      justify-content: center;
      margin-top: 18px;
    }}
    .dock-inner {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px;
      border-radius: 999px;
      border: 1px solid rgba(103,255,182,.18);
      background: rgba(5,16,13,.82);
      backdrop-filter: blur(18px);
      box-shadow: 0 18px 44px rgba(0,0,0,.28);
    }}
    .dock-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 84px;
      min-height: 42px;
      padding: 0 14px;
      border-radius: 999px;
      color: var(--text);
      text-decoration: none;
      font-size: 13px;
      font-weight: 800;
      background: rgba(255,255,255,.03);
      border: 1px solid transparent;
      transition: transform .18s ease, background .18s ease, border-color .18s ease;
    }}
    .dock-link:hover, .dock-link:active {{
      transform: translateY(-1px);
      background: rgba(103,255,182,.10);
      border-color: rgba(103,255,182,.18);
    }}
    .footer {{ margin-top: 14px; color: var(--muted); font-size: 13px; }}
    .site-footer {{
      margin-top: 24px;
      padding: 20px 22px;
      border: 1px solid rgba(103,255,182,.12);
      border-radius: 24px;
      background: rgba(7,18,16,.82);
      display: grid;
      grid-template-columns: 1.1fr .9fr;
      gap: 20px;
    }}
    .footer-brand h3 {{
      margin: 0;
      font-size: 24px;
      color: var(--accent);
    }}
    .footer-brand p, .footer-links a {{
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
      text-decoration: none;
    }}
    .footer-links {{
      display: grid;
      gap: 8px;
      justify-items: start;
    }}
    .reveal {{
      opacity: 0;
      transform: translateY(24px);
      animation: riseIn .7s cubic-bezier(.2,.8,.2,1) forwards;
    }}
    .float-in {{
      animation: floatIn 1s cubic-bezier(.16,1,.3,1) both;
    }}
    .match-list .reveal:nth-child(2n) {{ animation-delay: .06s; }}
    .match-list .reveal:nth-child(3n) {{ animation-delay: .12s; }}
    @keyframes riseIn {{
      from {{ opacity:0; transform: translateY(24px) scale(.985); }}
      to {{ opacity:1; transform: translateY(0) scale(1); }}
    }}
    @keyframes floatIn {{
      from {{ opacity: 0; transform: translateY(36px) scale(.96); }}
      to {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}
    @keyframes pulseOrbit {{
      0%, 100% {{ transform: translate3d(0, 0, 0) scale(1); opacity: .7; }}
      50% {{ transform: translate3d(-18px, -18px, 0) scale(1.08); opacity: 1; }}
    }}
    @keyframes glowPulse {{
      0%, 100% {{ box-shadow: 0 14px 26px rgba(103,255,182,.16); }}
      50% {{ box-shadow: 0 16px 34px rgba(103,255,182,.26); }}
    }}
    @media (max-width: 980px) {{
      .hero-stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .hero-strip {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      .section-grid {{ grid-template-columns: 1fr; }}
      .stats-grid {{ grid-template-columns: 1fr; }}
      .editorial-card {{ grid-template-columns: 1fr; }}
      .editorial-image {{ width: 100%; height: 220px; }}
      .spotlight-card h2, .editorial-copy h3 {{ font-size: 28px; }}
      .articles-grid, .favorites-grid {{ grid-template-columns: 1fr; }}
      .site-footer {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 720px) {{
      .wrap {{ padding: 16px 14px 104px; }}
      h1 {{ font-size: 34px; }}
      .toolbar {{ align-items: stretch; }}
      .field, input, .btn, .ghost-btn {{ width: 100%; }}
      .table-shell, .compact-table-shell {{ overflow-x: auto; }}
      .prediction-table {{ min-width: 760px; }}
      .hero-stats {{ grid-template-columns: 1fr 1fr; }}
      .bottom-dock {{ display: flex; }}
      .nav-links {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero float-in">
      <h1>GABFOOT Control</h1>
      <div class="sub">Application locale pour piloter les matchs surs, l'affiche Telegram et le scan des ligues populaires. Mise a jour: {now}</div>
      <div class="nav-links">
        <a class="nav-link" href="#top-picks">Top picks</a>
        <a class="nav-link" href="#botola-zone">Botola Pro</a>
        <a class="nav-link" href="#tennis-zone">Tennis World</a>
        <a class="nav-link" href="#articles-zone">Articles</a>
      </div>
      <div class="hero-stats">
        <div class="hero-stat reveal">
          <div class="hero-stat-label">Top picks</div>
          <div class="hero-stat-value">{len(matches)}</div>
          <div class="hero-stat-note">Matchs valides sur le filtre actuel</div>
        </div>
        <div class="hero-stat reveal">
          <div class="hero-stat-label">Fiabilite moyenne</div>
          <div class="hero-stat-value">{avg_confidence}%</div>
          <div class="hero-stat-note">Lecture instantanee des pronostics</div>
        </div>
        <div class="hero-stat reveal">
          <div class="hero-stat-label">Botola</div>
          <div class="hero-stat-value">{len(botola)}</div>
          <div class="hero-stat-note">Rencontres marocaines analysees</div>
        </div>
        <div class="hero-stat reveal">
          <div class="hero-stat-label">Articles</div>
          <div class="hero-stat-value">{len(articles)}</div>
          <div class="hero-stat-note">News illustrees disponibles</div>
        </div>
      </div>
      <div class="market-strip">
        {market_strip}
      </div>
      <form class="toolbar" method="get" action="/">
        <div class="field">
          <label for="limit">Nombre de matchs</label>
          <input id="limit" name="limit" type="number" min="1" max="12" value="{limit}">
        </div>
        <div class="field">
          <label for="min_percent">Seuil minimal</label>
          <input id="min_percent" name="min_percent" type="number" min="55" max="90" value="{min_percent}">
        </div>
        <button class="btn" type="submit">Actualiser</button>
        <a class="ghost-btn" href="/send?limit={limit}&min_percent={min_percent}">Envoyer sur Telegram</a>
      </form>
      {notice_html}
      <div class="hero-strip">
        {lead_panel}
        {editorial_panel}
      </div>
    </section>

    <div class="grid">
      <section id="top-picks" class="panel reveal section-anchor">
        <div class="panel-head">
          <div class="panel-title">
            <span class="panel-pill">Premium Board</span>
            <h2>Matchs les plus surs</h2>
          </div>
          <span class="footer">{len(matches)} match(s) retenu(s)</span>
        </div>
        {prediction_table}
      </section>
      {image_block}
    </div>

    <div class="section-grid">
      <section id="botola-zone" class="panel reveal section-anchor">
        <div class="panel-head">
          <div class="panel-title">
            <span class="panel-pill">Maroc</span>
            <h2>Pronostic Botola Pro</h2>
          </div>
          <span class="footer">Premiere ligue marocaine</span>
        </div>
        {botola_table}
      </section>
      <section id="tennis-zone" class="panel reveal section-anchor">
        <div class="panel-head">
          <div class="panel-title">
            <span class="panel-pill">Live Board</span>
            <h2>Tennis World</h2>
          </div>
          <span class="footer">ATP / WTA</span>
        </div>
        {tennis_table}
      </section>
    </div>

    <div class="section-grid">
      {favorites_panel}
    </div>

    <div class="section-grid">
      <section id="articles-zone" class="panel reveal section-anchor" style="grid-column: 1 / -1;">
        <div class="panel-head">
          <div class="panel-title">
            <span class="panel-pill">Editorial</span>
            <h2>Articles clubs & joueurs</h2>
          </div>
          <span class="footer">Actualites football</span>
        </div>
        <div class="articles-grid">
          {article_cards}
        </div>
      </section>
    </div>

    <nav class="bottom-dock">
      <div class="dock-inner">
        <a class="dock-link" href="#top-picks">Picks</a>
        <a class="dock-link" href="#favorites-zone">Favoris</a>
        <a class="dock-link" href="#botola-zone">Botola</a>
        <a class="dock-link" href="#tennis-zone">Tennis</a>
        <a class="dock-link" href="#articles-zone">News</a>
      </div>
    </nav>

    <footer class="site-footer reveal">
      <div class="footer-brand">
        <h3>GABFOOT</h3>
        <p>Plateforme pronostics foot premium, matchs classes par heure, lecture rapide des signaux et actualites clubs & joueurs dans une seule experience web.</p>
      </div>
      <div class="footer-links">
        <a href="#top-picks">Top picks</a>
        <a href="#favorites-zone">Mes favoris</a>
        <a href="#botola-zone">Pronostics Botola</a>
        <a href="#tennis-zone">Tennis World</a>
        <a href="#articles-zone">Articles football</a>
      </div>
    </footer>
  </div>
</body>
{interaction_script()}
{favorites_script()}
<script>
if ('serviceWorker' in navigator) {{
  window.addEventListener('load', function () {{
    navigator.serviceWorker.register('/sw.js').catch(function () {{}});
  }});
}}
</script>
</html>"""


def match_to_dict(match: InterestingMatch) -> dict[str, object]:
    return {
        "tournament": match.tournament_name,
        "kickoff": fmt_kickoff(match.kickoff_utc),
        "homeName": match.home_name,
        "awayName": match.away_name,
        "prediction": match.prediction,
        "surenessPercent": match.sureness_percent,
        "homeForm": match.home_form,
        "awayForm": match.away_form,
        "homeRank": match.home_rank,
        "awayRank": match.away_rank,
        "homeHistoryScore": match.home_history_score,
        "awayHistoryScore": match.away_history_score,
        "h2hEdge": match.h2h_edge,
        "injuriesEdge": match.injuries_edge,
        "homeKeyPlayer": match.home_key_player,
        "awayKeyPlayer": match.away_key_player,
        "why": match.why,
        "consensusNotes": match.consensus_notes,
    }


def render_collection(title: str, subtitle: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - GABFOOT</title>
  <style>
    body {{
      margin:0; font-family:"DejaVu Sans", sans-serif; color:#edf6ee;
      background:
        radial-gradient(circle at top left, rgba(87, 190, 98, 0.16), transparent 24%),
        radial-gradient(circle at 85% 10%, rgba(197, 255, 120, 0.10), transparent 18%),
        linear-gradient(180deg, #050c08 0%, #0b1711 48%, #07120b 100%);
    }}
    .wrap {{ max-width:1100px; margin:0 auto; padding:24px 18px 52px; }}
    .hero, .panel {{
      border:1px solid rgba(119,255,162,.18); border-radius:28px; background:rgba(11,20,16,.82);
      box-shadow:0 22px 60px rgba(0,0,0,.32); backdrop-filter: blur(16px);
    }}
    .hero {{ padding:24px; position:relative; overflow:hidden; }}
    .hero::before {{
      content:""; position:absolute; inset:0;
      background:
        radial-gradient(circle at 20% 0%, rgba(124,255,107,.18), transparent 28%),
        radial-gradient(circle at 100% 10%, rgba(116,245,255,.10), transparent 22%);
      pointer-events:none;
    }}
    h1 {{ margin:0; font-size:38px; color:#8cff72; position:relative; z-index:1; }}
    .sub {{ margin-top:8px; color:#aebcaf; position:relative; z-index:1; }}
    .links {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:18px; }}
    .links a {{
      color:#edf6ee; text-decoration:none; border:1px solid rgba(119,255,162,.14); border-radius:999px; padding:10px 14px;
      background:rgba(255,255,255,.04); transition: transform .18s ease, background .18s ease, border-color .18s ease;
      position:relative; z-index:1;
    }}
    .links a:hover, .links a:active {{ transform:translateY(-1px); background:rgba(124,255,107,.10); border-color:rgba(119,255,162,.28); }}
    .panel {{ padding:18px; margin-top:18px; }}
    .match-list {{ display:grid; gap:14px; }}
    .match-card {{
      padding:17px; border-radius:22px; background:rgba(16,28,21,.94); border:1px solid rgba(124,255,107,.10);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.03); transition: transform .18s ease, border-color .18s ease;
    }}
    .match-card:hover {{ transform:translateY(-2px); border-color:rgba(124,255,107,.18); }}
    .meta-row {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; }}
    .league {{ color:#8cff72; font-size:14px; font-weight:700; }}
    .kickoff {{ color:#aebcaf; font-size:13px; margin-top:4px; }}
    .pick-badge {{ min-width:92px; border-radius:18px; background:linear-gradient(180deg, rgba(19,38,23,.95), rgba(13,27,17,.95)); padding:8px 10px; border:1px solid rgba(124,255,107,.22); text-align:center; }}
    .pick-value {{ display:block; font-size:24px; font-weight:800; color:#8cff72; }}
    .pick-percent {{ display:block; font-size:13px; color:#aebcaf; }}
    .teams, .article-title {{ margin-top:12px; font-size:24px; font-weight:700; }}
    .teams span {{ color:#aebcaf; font-size:16px; font-weight:500; }}
    .stats-grid {{ margin-top:14px; display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:10px; }}
    .stats-grid div {{ padding:11px 12px; border-radius:16px; background:rgba(255,255,255,.035); border:1px solid rgba(140,255,114,.08); }}
    .stats-grid strong {{ display:block; color:#d8ff7a; font-size:12px; margin-bottom:5px; }}
    .stats-grid span {{ display:block; color:#edf6ee; font-size:14px; line-height:1.35; }}
    .why {{ margin-top:12px; font-size:14px; color:#aebcaf; }}
    .tennis-state {{ min-width:110px; padding:8px 10px; border-radius:14px; text-align:center; color:#d8ff7a; background:#132617; border:1px solid #2f5b39; font-size:13px; font-weight:700; }}
    .article-link {{ display:inline-flex; margin-top:14px; color:#8cff72; text-decoration:none; font-weight:800; }}
    @media (max-width: 980px) {{
      .stats-grid {{ grid-template-columns:1fr; }}
      .teams, .article-title {{ font-size:20px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>{html.escape(title)}</h1>
      <div class="sub">{html.escape(subtitle)}</div>
      <div class="links">
        <a href="/">Accueil</a>
        <a href="/botola">Botola Pro</a>
        <a href="/tennis">Tennis World</a>
        <a href="/articles">Articles clubs & joueurs</a>
      </div>
    </section>
    <section class="panel">
      <div class="match-list">{content}</div>
    </section>
  </div>
</body>
{interaction_script()}
</html>"""


def render_botola_cards(botola: list[dict[str, object]]) -> str:
    return render_botola_table(botola)


def render_tennis_cards(tennis: list[dict[str, object]]) -> str:
    return render_tennis_table(tennis)


def render_article_cards(articles: list[dict[str, str]]) -> str:
    cards = []
    for item in articles:
        link = ""
        if item["url"]:
            link = f'<a class="article-link" href="{html.escape(item["url"])}" target="_blank" rel="noreferrer">Lire l&apos;article</a>'
        image = str(item.get("image", "")).strip()
        visual = (
            f'<img src="{html.escape(image)}" alt="{html.escape(item["title"])}" style="width:100%;height:220px;object-fit:cover;border-radius:18px;">'
            if image
            else '<div style="width:100%;height:220px;border-radius:18px;background:rgba(255,255,255,.04);display:flex;align-items:center;justify-content:center;color:#d8ff7a;font-weight:900;letter-spacing:.12em;">GABFOOT</div>'
        )
        cards.append(
            f"""
            <article class="match-card">
              <div>{visual}</div>
              <div class="meta-row">
                <div>
                  <div class="league">Football News</div>
                  <div class="kickoff">{html.escape(item['team'])} • {html.escape(item['source'])}</div>
                </div>
              </div>
              <div class="article-title">{html.escape(item['title'])}</div>
              <div class="why">{html.escape(item['summary'])}</div>
              {link}
            </article>
            """
        )
    return "".join(cards) or '<div class="why">Aucun article remonte pour le moment.</div>'


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        limit = max(1, min(12, int(params.get("limit", ["6"])[0])))
        min_percent = max(55, min(90, int(params.get("min_percent", ["78"])[0])))
        force_refresh = "_refresh" in params or params.get("force", ["0"])[0] == "1"
        if force_refresh:
            cached_payload = get_fresh_dashboard_payload(limit=limit, min_percent=min_percent)
            stale = False
        else:
            cached_payload, stale = get_cached_dashboard_payload(limit=limit, min_percent=min_percent)

        if parsed.path == "/image":
            return self.serve_image()

        if parsed.path == "/icon.png":
            return self.serve_file(ICON_PATH, "image/png")

        if parsed.path == "/manifest.webmanifest":
            return self.render_manifest()

        if parsed.path == "/sw.js":
            return self.render_sw()

        if parsed.path == "/api/dashboard":
            if cached_payload is None:
                return self.render_json(
                    {
                        "updatedAt": "",
                        "minPercent": min_percent,
                        "imagePath": "/image" if CARD_PATH.exists() else "",
                        "matches": [],
                        "botolaPredictions": [],
                        "tennisWorld": [],
                        "footballArticles": [],
                        "notice": "Chargement initial en cours. Recharge dans quelques secondes.",
                    }
                )
            matches = [deserialize_match(item) for item in cached_payload.get("matches", [])]
            return self.render_json(
                {
                    "updatedAt": datetime.fromtimestamp(float(cached_payload.get("updated_at", time.time())), timezone.utc).isoformat(),
                    "minPercent": min_percent,
                    "imagePath": "/image" if cached_payload.get("has_card") else "",
                    "matches": [match_to_dict(match) for match in matches],
                    "botolaPredictions": cached_payload.get("botola", []),
                    "tennisWorld": cached_payload.get("tennis", []),
                    "footballArticles": cached_payload.get("articles", []),
                    "notice": "Donnees en cache, actualisation en cours." if stale else "",
                }
            )

        if parsed.path.startswith("/match/"):
            raw_match_id = parsed.path.removeprefix("/match/").strip("/")
            if not raw_match_id.isdigit():
                self.send_error(HTTPStatus.NOT_FOUND, "Match introuvable")
                return
            match = find_match_for_detail(int(raw_match_id))
            if not match:
                self.send_error(HTTPStatus.NOT_FOUND, "Match introuvable")
                return
            articles = safe_football_articles([match], [], limit=6)
            return self.render_html(render_match_detail_page(match, articles))

        if parsed.path.startswith("/team/"):
            raw_team_id = parsed.path.removeprefix("/team/").strip("/")
            if not raw_team_id.isdigit():
                self.send_error(HTTPStatus.NOT_FOUND, "Equipe introuvable")
                return
            return self.render_html(render_team_detail_page(int(raw_team_id)))

        if parsed.path.startswith("/league/"):
            raw_league_id = parsed.path.removeprefix("/league/").strip("/")
            if not raw_league_id.isdigit():
                self.send_error(HTTPStatus.NOT_FOUND, "Ligue introuvable")
                return
            return self.render_html(render_league_detail_page(int(raw_league_id)))

        if parsed.path == "/botola":
            botola = safe_botola_predictions(limit=12)
            return self.render_html(render_collection("Pronostic Botola Pro", "Premiere ligue marocaine", render_botola_cards(botola)))

        if parsed.path == "/tennis":
            tennis = safe_tennis_world_matches(limit=16)
            return self.render_html(render_collection("Tennis World", "Circuit ATP / WTA", render_tennis_cards(tennis)))

        if parsed.path == "/articles":
            matches, _ = build_card(limit=4, min_percent=min_percent)
            botola = safe_botola_predictions(limit=6)
            articles = safe_football_articles(matches, botola, limit=18)
            return self.render_html(render_collection("Articles clubs & joueurs", "Actualites football, clubs et joueurs", render_article_cards(articles)))

        if parsed.path == "/api/send":
            matches, card_path = build_card(limit=limit, min_percent=min_percent)
            if not card_path:
                return self.render_json({"ok": False, "message": "Aucun match disponible"}, status=HTTPStatus.OK)
            send_photo(card_path, f"Top du top | Matchs surs {min_percent}%+")
            return self.render_json({"ok": True, "message": "Affiche envoyee sur Telegram"})

        if parsed.path == "/send":
            matches, card_path = build_card(limit=limit, min_percent=min_percent)
            botola = safe_botola_predictions()
            tennis = safe_tennis_world_matches()
            articles = safe_football_articles(matches, botola)
            notice = "Aucun match disponible a envoyer."
            if card_path:
                send_photo(card_path, f"Top du top | Matchs surs {min_percent}%+")
                notice = "Affiche envoyee sur Telegram."
            return self.render_dashboard(matches, card_path, limit, min_percent, notice, botola, tennis, articles)

        if cached_payload is None:
            notice = "Chargement initial en cours. Recharge la page dans quelques secondes."
            return self.render_dashboard([], CARD_PATH if CARD_PATH.exists() else None, limit, min_percent, notice, [], [], [])

        matches = [deserialize_match(item) for item in cached_payload.get("matches", [])]
        card_path = CARD_PATH if cached_payload.get("has_card") and CARD_PATH.exists() else None
        notice = "Donnees en cache, actualisation en cours." if stale else ""
        return self.render_dashboard(
            matches,
            card_path,
            limit,
            min_percent,
            notice,
            list(cached_payload.get("botola", [])),
            list(cached_payload.get("tennis", [])),
            list(cached_payload.get("articles", [])),
        )

    def serve_image(self) -> None:
        return self.serve_file(CARD_PATH, "image/png")

    def serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Fichier indisponible")
            return
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(payload)

    def render_dashboard(
        self,
        matches: list[InterestingMatch],
        card_path: Path | None,
        limit: int,
        min_percent: int,
        notice: str = "",
        botola: list[dict[str, object]] | None = None,
        tennis: list[dict[str, object]] | None = None,
        articles: list[dict[str, str]] | None = None,
    ) -> None:
        payload = page_html(matches, card_path, limit, min_percent, notice, botola, tennis, articles).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(payload)

    def render_html(self, html_text: str) -> None:
        payload = html_text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(payload)

    def render_json(self, data: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(payload)

    def render_manifest(self) -> None:
        payload = json.dumps(
            {
                "name": "GABFOOT",
                "short_name": "GABFOOT",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#08150c",
                "theme_color": "#08150c",
                "description": "Matchs surs, affiche Telegram et controle GABFOOT.",
                "icons": [
                    {
                        "src": "/icon.png",
                        "sizes": "512x512",
                        "type": "image/png",
                    }
                ],
            },
            ensure_ascii=True,
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def render_sw(self) -> None:
        payload = b"""self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> int:
    load_dotenv(ENV_FILE)
    load_dashboard_cache()
    ensure_dashboard_refresh(limit=6, min_percent=78)
    port = int(os.getenv("PORT") or os.getenv("GABFOOT_WEB_PORT", str(PORT)))
    default_host = "0.0.0.0" if os.getenv("PORT") else HOST
    host = os.getenv("GABFOOT_WEB_HOST", default_host)
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"GABFOOT web app sur http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
