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
SITE_ASSETS_DIR = Path(__file__).resolve().parent / "site_assets"
DASHBOARD_HERO_IMAGE_PATH = SITE_ASSETS_DIR / "dashboard-hero.jpg"
BOTOLA_LEAGUE_ID = 530
TEAM_CACHE: dict[int, dict] = {}
APP_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
DASHBOARD_CACHE_FILE = APP_CACHE_DIR / "dashboard_cache.json"
PREFERRED_PUBLIC_URL_FILE = APP_CACHE_DIR / "preferred_public_url.txt"
DASHBOARD_CACHE_TTL_SECONDS = 15 * 60
DASHBOARD_CACHE_STALE_SECONDS = 6 * 3600
_DASHBOARD_CACHE: dict[str, dict[str, object]] = {}
_CACHE_LOCK = threading.Lock()
_REFRESH_IN_FLIGHT: set[str] = set()
ARTICLE_IMAGE_CACHE: dict[str, str] = {}
ARTICLE_IMAGE_LOCK = threading.Lock()
ARTICLE_IMAGE_FETCH_ENABLED = os.getenv("GABFOOT_FETCH_ARTICLE_IMAGES", "").strip() == "1"
UPDATES_FILE = Path(__file__).resolve().parent / "updates.json"


def configured_public_url() -> str:
    candidates = [
        os.getenv("GABFOOT_PUBLIC_URL", "").strip(),
        os.getenv("GABFOOT_SITE_URL", "").strip(),
    ]
    if PREFERRED_PUBLIC_URL_FILE.exists():
        try:
            candidates.append(PREFERRED_PUBLIC_URL_FILE.read_text().strip())
        except OSError:
            pass
    for candidate in candidates:
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate.rstrip("/")
    return ""


def load_site_updates() -> list[dict[str, object]]:
    try:
        payload = json.loads(UPDATES_FILE.read_text(encoding="utf-8"))
    except OSError:
        return []
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []

    cleaned: list[dict[str, object]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        bullets = entry.get("bullets", [])
        if not isinstance(bullets, list):
            bullets = []
        cleaned.append(
            {
                "date": str(entry.get("date", "")).strip(),
                "label": str(entry.get("label", "Produit")).strip() or "Produit",
                "title": str(entry.get("title", "")).strip(),
                "summary": str(entry.get("summary", "")).strip(),
                "bullets": [str(bullet).strip() for bullet in bullets if str(bullet).strip()],
            }
        )
    return cleaned


def request_scheme(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if forwarded in {"http", "https"}:
        return forwarded
    if os.getenv("PORT"):
        return "https"
    return "http"


def request_host(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    host = handler.headers.get("Host", "").split(",")[0].strip()
    if host:
        return host
    return f"{HOST}:{PORT}"


def request_base_url(handler: BaseHTTPRequestHandler) -> str:
    configured = configured_public_url()
    if configured:
        return configured
    return f"{request_scheme(handler)}://{request_host(handler)}"


def absolute_url(base_url: str, path: str) -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    return f"{base_url.rstrip('/')}{normalized}"


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

    # Avoid blocking public routes on third-party article pages unless explicitly enabled.
    if not ARTICLE_IMAGE_FETCH_ENABLED:
        return fallback

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
        with urlopen(request, timeout=3) as response:
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
            payload.get("details", {}).get("sportsTeamJSONLD", {}).get("logo")
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

  const revealItems = Array.from(document.querySelectorAll('.reveal'));
  if ('IntersectionObserver' in window && revealItems.length) {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('in-view');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.14, rootMargin: '0px 0px -40px 0px' });
    revealItems.forEach((item, index) => {
      item.style.setProperty('--reveal-delay', `${Math.min(index * 40, 280)}ms`);
      observer.observe(item);
    });
  } else {
    revealItems.forEach((item) => item.classList.add('in-view'));
  }
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


def render_landing_pick_cards(matches: list[InterestingMatch]) -> str:
    cards: list[str] = []
    for match in matches[:3]:
        consensus = " / ".join(match.consensus_notes[:2]) if match.consensus_notes else "Lecture premium en cours."
        why = " • ".join(match.why[:2]) if match.why else "Forme, classement et momentum croises."
        cards.append(
            f"""
            <article class="landing-pick-card reveal {pick_theme(match.prediction)}">
              <div class="landing-pick-top">
                <div>
                  <div class="landing-kickoff">{html.escape(fmt_kickoff(match.kickoff_utc))}</div>
                  <h3>{html.escape(match.home_name)} <span>vs</span> {html.escape(match.away_name)}</h3>
                  <div class="landing-league">{html.escape(match.tournament_name)}</div>
                </div>
                {render_pick_badge(match.prediction, match.sureness_percent)}
              </div>
              <p>{html.escape(why)}</p>
              <div class="landing-pick-meta">
                <span>{html.escape(consensus)}</span>
                <a href="/match/{match.match_id}">Voir la fiche</a>
              </div>
            </article>
            """
        )
    return "".join(cards) or '<div class="landing-empty">Les premiers signaux apparaitront ici des que le prochain scan sera termine.</div>'


def render_landing_article_teasers(articles: list[dict[str, str]]) -> str:
    cards: list[str] = []
    for item in articles[:3]:
        image = str(item.get("image", "")).strip()
        visual = (
            f'<img class="landing-article-image" src="{html.escape(image)}" alt="{html.escape(item["title"])}">'
            if image
            else '<div class="landing-article-image landing-article-fallback">NEWS</div>'
        )
        link = (
            f'<a class="landing-article-link" href="{html.escape(item["url"])}" target="_blank" rel="noreferrer">Lire</a>'
            if item.get("url")
            else ""
        )
        cards.append(
            f"""
            <article class="landing-article-card reveal">
              {visual}
              <div class="landing-article-copy">
                <div class="landing-article-source">{html.escape(item.get("source", "Football News"))}</div>
                <h3>{html.escape(item["title"])}</h3>
                <p>{html.escape(item["summary"])}</p>
                <div class="landing-article-bottom">
                  <span>{html.escape(item["team"])}</span>
                  {link}
                </div>
              </div>
            </article>
            """
        )
    return "".join(cards) or '<div class="landing-empty">Les articles clubs et joueurs seront affiches ici.</div>'


def render_landing_update_teasers(updates: list[dict[str, object]]) -> str:
    cards: list[str] = []
    for item in updates[:3]:
        bullets = item.get("bullets", [])
        bullet_line = " • ".join(str(bullet) for bullet in bullets[:2])
        cards.append(
            f"""
            <article class="landing-article-card reveal">
              <div class="landing-article-image landing-article-fallback">UPDATE</div>
              <div class="landing-article-copy">
                <div class="landing-article-source">{html.escape(str(item.get("label", "Produit")))} • {html.escape(str(item.get("date", "")))}</div>
                <h3>{html.escape(str(item.get("title", "")))}</h3>
                <p>{html.escape(str(item.get("summary", "")))}</p>
                <div class="landing-article-bottom">
                  <span>{html.escape(bullet_line)}</span>
                  <a class="landing-article-link" href="/updates">Voir</a>
                </div>
              </div>
            </article>
            """
        )
    return "".join(cards) or '<div class="landing-empty">Les prochaines mises a jour du produit apparaitront ici.</div>'


def landing_html(
    matches: list[InterestingMatch],
    card_path: Path | None,
    limit: int,
    min_percent: int,
    notice: str = "",
    botola: list[dict[str, object]] | None = None,
    tennis: list[dict[str, object]] | None = None,
    articles: list[dict[str, str]] | None = None,
    site_url: str = "",
) -> str:
    site_updates = load_site_updates()
    botola = botola or []
    tennis = tennis or []
    articles = articles or []
    featured_match = matches[0] if matches else None
    featured_article = articles[0] if articles else None
    avg_confidence = int(round(sum(match.sureness_percent for match in matches) / len(matches))) if matches else 0
    updated_at = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    site_url = site_url.rstrip("/")
    canonical_url = f"{site_url}/" if site_url else ""
    icon_url = absolute_url(site_url, "/icon.png") if site_url else "/icon.png"
    canonical_tag = f'<link rel="canonical" href="{html.escape(canonical_url)}">' if canonical_url else ""
    og_url_tag = f'<meta property="og:url" content="{html.escape(canonical_url)}">' if canonical_url else ""
    notice_html = f'<div class="alert-banner">{html.escape(notice)}</div>' if notice else ""
    poster_visual = (
        '<img class="poster-visual" src="/image" alt="Affiche Telegram GABFOOT">'
        if card_path and card_path.exists()
        else '<div class="poster-visual poster-visual-fallback">Poster Telegram pret a sortir</div>'
    )
    market_strip = render_market_strip(matches, botola)
    featured_match_html = ""
    if featured_match:
        featured_match_html = f"""
        <article class="hero-card signal-card">
          <div class="mini-label">Signal principal</div>
          <h3>{html.escape(featured_match.home_name)} <span>vs</span> {html.escape(featured_match.away_name)}</h3>
          <div class="signal-meta">{html.escape(featured_match.tournament_name)} • {html.escape(fmt_kickoff(featured_match.kickoff_utc))}</div>
          <div class="signal-pill-row">
            <span>{html.escape(featured_match.prediction)} • {featured_match.sureness_percent}%</span>
            <span>{html.escape(featured_match.home_form)} | {html.escape(featured_match.away_form)}</span>
          </div>
          <p>{html.escape(' • '.join(featured_match.why[:3]))}</p>
          <a class="outline-btn" href="/match/{featured_match.match_id}">Voir la lecture complete</a>
        </article>
        """
    featured_article_html = ""
    if featured_article:
        article_image = str(featured_article.get("image", "")).strip()
        article_visual = (
            f'<img class="editorial-thumb" src="{html.escape(article_image)}" alt="{html.escape(featured_article["title"])}">'
            if article_image
            else '<div class="editorial-thumb editorial-thumb-fallback">NEWS</div>'
        )
        article_link = (
            f'<a class="outline-btn" href="{html.escape(featured_article["url"])}" target="_blank" rel="noreferrer">Lire l&apos;article</a>'
            if featured_article.get("url")
            else ""
        )
        featured_article_html = f"""
        <article class="hero-card editorial-card">
          {article_visual}
          <div class="editorial-copy">
            <div class="mini-label">Pulse editorial</div>
            <h3>{html.escape(featured_article["title"])}</h3>
            <p>{html.escape(featured_article["summary"])}</p>
            <div class="signal-meta">{html.escape(featured_article["team"])} • {html.escape(featured_article["source"])}</div>
            {article_link}
          </div>
        </article>
        """

    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GABFOOT | Studio Pronostics Foot</title>
  <meta name="description" content="GABFOOT assemble une lecture premium des pronostics foot, des matchs les plus surs, de la Botola et des actualites dans une interface plus claire et plus moderne.">
  <meta property="og:title" content="GABFOOT | Studio Pronostics Foot">
  <meta property="og:description" content="Une vitrine premium pour ouvrir le dashboard GABFOOT, suivre les picks les plus solides et acceder aux flux Botola, tennis et editorial.">
  <meta property="og:type" content="website">
  <meta property="og:image" content="{html.escape(icon_url)}">
  {og_url_tag}
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="GABFOOT | Studio Pronostics Foot">
  <meta name="twitter:description" content="Un style plus premium pour les pronostics GABFOOT, la Botola, le dashboard live et le poster Telegram.">
  <meta name="twitter:image" content="{html.escape(icon_url)}">
  <meta name="theme-color" content="#183326">
  {canonical_tag}
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="icon" type="image/png" href="/icon.png">
  <link rel="apple-touch-icon" href="/icon.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@400;500;700;800&family=Source+Sans+3:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --canvas: #f4f0e7;
      --canvas-2: #efe5d4;
      --paper: rgba(255, 252, 246, 0.82);
      --paper-strong: #fffdf8;
      --ink: #10281f;
      --ink-soft: #5f6f66;
      --forest: #1e5b43;
      --forest-deep: #14392b;
      --gold: #c89a2b;
      --terracotta: #c4512d;
      --line: rgba(16, 40, 31, 0.10);
      --line-strong: rgba(16, 40, 31, 0.18);
      --shadow: 0 24px 60px rgba(19, 39, 30, 0.12);
      --radius-xl: 34px;
      --radius-lg: 26px;
      --radius-md: 20px;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      font-family: "Source Sans 3", "DejaVu Sans", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 0% 0%, rgba(200,154,43,.18), transparent 28%),
        radial-gradient(circle at 100% 10%, rgba(30,91,67,.12), transparent 22%),
        linear-gradient(180deg, var(--canvas) 0%, var(--canvas-2) 100%);
    }}
    a {{ color: inherit; }}
    .site-shell {{ max-width: 1380px; margin: 0 auto; padding: 12px 20px 52px; }}
    .site-topbar {{
      display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom: 16px;
      padding: 14px 18px; border-radius: 24px; border:1px solid rgba(255,255,255,.48);
      background: rgba(255,255,255,.56); backdrop-filter: blur(14px);
      box-shadow: 0 16px 34px rgba(20,57,43,.08);
    }}
    .site-brand {{ display:flex; align-items:center; gap:14px; text-decoration:none; color:var(--ink); }}
    .site-brand-mark {{
      width:52px; height:52px; border-radius:16px; display:grid; place-items:center;
      font-family:"Bricolage Grotesque", sans-serif; font-size:24px; font-weight:800;
      background:linear-gradient(135deg, var(--forest), var(--gold)); color:#fff7ea; box-shadow: var(--shadow);
    }}
    .site-brand-copy strong {{
      display:block; font-family:"Bricolage Grotesque", sans-serif; font-size:24px; line-height:1; letter-spacing:.02em;
    }}
    .site-brand-copy span {{ display:block; margin-top:4px; color:var(--ink-soft); font-size:13px; line-height:1.45; }}
    .site-nav {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }}
    .site-nav a {{
      min-height:42px; padding:0 16px; display:inline-flex; align-items:center; justify-content:center;
      border-radius:999px; text-decoration:none; font-size:13px; font-weight:800;
      border:1px solid var(--line); background:rgba(255,255,255,.64);
      transition:transform .18s ease, border-color .18s ease, background .18s ease;
    }}
    .site-nav a:hover {{ transform:translateY(-1px); border-color:var(--line-strong); background:var(--paper-strong); }}
    .site-nav .site-nav-cta {{
      color:#fff7ea; border-color:transparent;
      background:linear-gradient(90deg, var(--forest), var(--gold));
      box-shadow: 0 16px 28px rgba(30,91,67,.18);
    }}
    .hero-stage {{
      position:relative; overflow:hidden; display:grid; grid-template-columns: minmax(0, 1.14fr) minmax(360px, .86fr); gap:28px; align-items:start;
      padding:28px; border-radius:var(--radius-xl); border:1px solid rgba(255,255,255,.50);
      background:
        radial-gradient(circle at 12% 0%, rgba(200,154,43,.16), transparent 28%),
        radial-gradient(circle at 100% 16%, rgba(30,91,67,.16), transparent 22%),
        radial-gradient(circle at 50% 100%, rgba(78,136,93,.14), transparent 32%),
        linear-gradient(135deg, rgba(255,251,245,.96), rgba(246,236,221,.98));
      box-shadow: var(--shadow);
    }}
    .hero-stage::before {{
      content:""; position:absolute; inset:auto -90px -90px auto; width:260px; height:260px; border-radius:50%;
      background: radial-gradient(circle, rgba(30,91,67,.12), transparent 68%);
      pointer-events:none;
      animation: driftGlow 6s ease-in-out infinite;
    }}
    .hero-stage::after {{
      content:""; position:absolute; inset:0; pointer-events:none; opacity:.52;
      background:
        radial-gradient(circle at 50% 102%, rgba(18,71,52,.18), transparent 24%),
        linear-gradient(90deg, transparent 14%, rgba(30,91,67,.10) 14.4%, rgba(30,91,67,.10) 14.8%, transparent 15.2%, transparent 84.8%, rgba(30,91,67,.10) 85.2%, rgba(30,91,67,.10) 85.6%, transparent 86%),
        linear-gradient(transparent 76%, rgba(30,91,67,.10) 76.4%, rgba(30,91,67,.10) 76.8%, transparent 77.2%),
        radial-gradient(circle at 50% 76%, transparent 0 74px, rgba(30,91,67,.10) 75px, rgba(30,91,67,.10) 77px, transparent 78px);
    }}
    .hero-copy, .hero-side {{ position:relative; z-index:1; }}
    .hero-copy {{ display:flex; flex-direction:column; gap:16px; justify-content:space-between; min-width:0; }}
    .hero-decor {{
      position:absolute; inset:0; pointer-events:none; z-index:0;
    }}
    .hero-ball {{
      position:absolute; right:30px; top:26px; width:74px; height:74px; border-radius:50%;
      background:
        radial-gradient(circle at 32% 30%, rgba(255,255,255,.92), rgba(244,242,236,.98) 54%, rgba(212,208,197,.96) 100%);
      box-shadow:
        inset -10px -14px 20px rgba(0,0,0,.08),
        0 20px 32px rgba(20,57,43,.16);
      animation: floatBall 7s ease-in-out infinite;
    }}
    .hero-ball::before {{
      content:""; position:absolute; inset:18px; border-radius:50%;
      background:
        radial-gradient(circle at 50% 50%, rgba(17,39,30,.88) 0 18%, transparent 19%),
        linear-gradient(36deg, transparent 44%, rgba(17,39,30,.88) 45% 49%, transparent 50%),
        linear-gradient(-38deg, transparent 44%, rgba(17,39,30,.88) 45% 49%, transparent 50%);
      opacity:.92;
    }}
    .hero-glow {{
      position:absolute; left:-40px; bottom:-60px; width:320px; height:180px; border-radius:50%;
      background:radial-gradient(circle, rgba(200,154,43,.20), transparent 68%);
      filter:blur(8px);
      animation: pulseGlow 8s ease-in-out infinite;
    }}
    .eyebrow {{
      display:inline-flex; width:max-content; min-height:34px; align-items:center; padding:0 12px;
      border-radius:999px; background:rgba(200,154,43,.14); color:#885f1b;
      font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.08em;
    }}
    .hero-copy h1 {{
      margin:0; font-family:"Bricolage Grotesque", sans-serif;
      font-size:clamp(3rem, 6vw, 5.4rem); line-height:.9; letter-spacing:-.03em; max-width: 720px;
    }}
    .hero-copy p {{
      margin:0; max-width: 660px; color:var(--ink-soft); font-size:17px; line-height:1.58;
    }}
    .hero-actions {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; }}
    .hero-rail {{
      display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px;
    }}
    .hero-rail-item {{
      padding:14px 16px; border-radius:20px; border:1px solid rgba(16,40,31,.10);
      background:linear-gradient(180deg, rgba(255,255,255,.82), rgba(255,249,240,.88));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.68);
    }}
    .hero-rail-item strong {{
      display:block; font-size:15px; font-weight:800; letter-spacing:-.01em;
    }}
    .hero-rail-item span {{
      display:block; margin-top:6px; color:var(--ink-soft); font-size:13px; line-height:1.5;
    }}
    .site-btn, .ghost-site-btn, .outline-btn {{
      min-height:50px; padding:0 18px; border-radius:16px; display:inline-flex; align-items:center; justify-content:center;
      text-decoration:none; font-weight:800; transition:transform .18s ease, box-shadow .18s ease, background .18s ease, border-color .18s ease;
    }}
    .site-btn {{
      color:#fff7ea; background:linear-gradient(90deg, var(--forest), var(--gold));
      box-shadow: 0 16px 32px rgba(30,91,67,.18);
    }}
    .ghost-site-btn, .outline-btn {{
      background:rgba(255,255,255,.72); border:1px solid var(--line); color:var(--ink);
    }}
    .site-btn:hover, .ghost-site-btn:hover, .outline-btn:hover {{ transform:translateY(-1px); }}
    .hero-facts {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:12px; }}
    .hero-fact {{
      padding:14px; border-radius:22px; border:1px solid rgba(255,255,255,.48);
      background:linear-gradient(180deg, rgba(255,255,255,.72), rgba(255,255,255,.56));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.56);
    }}
    .hero-fact strong {{
      display:block; font-family:"Bricolage Grotesque", sans-serif; font-size:30px; line-height:1; color:var(--forest);
    }}
    .hero-fact span {{ display:block; margin-top:8px; color:var(--ink-soft); font-size:14px; line-height:1.45; }}
    .market-strip {{
      display:flex; gap:10px; overflow-x:auto; padding-bottom:4px; scrollbar-width:none;
    }}
    .market-strip::-webkit-scrollbar {{ display:none; }}
    .market-pill {{
      min-width:220px; padding:14px 16px; border-radius:18px; border:1px solid rgba(16,40,31,.10);
      background:rgba(255,255,255,.72); box-shadow: inset 0 1px 0 rgba(255,255,255,.56);
    }}
    .market-pill-label {{
      display:block; color:var(--ink-soft); font-size:11px; font-weight:800; text-transform:uppercase; letter-spacing:.08em;
    }}
    .market-pill-value {{
      display:block; margin-top:8px; font-size:18px; font-weight:800; color:var(--ink);
    }}
    .alert-banner {{
      padding:14px 16px; border-radius:18px; border:1px solid rgba(196,81,45,.18);
      background:rgba(196,81,45,.08); color:#8c4529; font-size:14px; line-height:1.55;
    }}
    .hero-side {{ display:grid; gap:16px; align-content:start; min-width:0; }}
    .hero-card {{
      border-radius:var(--radius-lg); border:1px solid rgba(255,255,255,.52);
      background:linear-gradient(180deg, rgba(255,255,255,.78), rgba(255,255,255,.58));
      box-shadow: var(--shadow); backdrop-filter: blur(10px);
    }}
    .signal-card, .poster-card {{ padding:20px; }}
    .mini-label {{
      color:#8a611d; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.08em;
    }}
    .signal-card h3, .poster-copy h3, .editorial-copy h3 {{
      margin:12px 0 8px; font-family:"Bricolage Grotesque", sans-serif; font-size:30px; line-height:1; letter-spacing:-.03em;
    }}
    .signal-card h3 span {{ color:var(--ink-soft); font-size:18px; font-weight:700; }}
    .signal-meta {{ color:var(--ink-soft); font-size:13px; line-height:1.5; }}
    .signal-pill-row {{
      display:flex; gap:8px; flex-wrap:wrap; margin-top:14px;
    }}
    .signal-pill-row span {{
      min-height:34px; display:inline-flex; align-items:center; padding:0 12px; border-radius:999px;
      background:rgba(30,91,67,.08); color:var(--forest); font-size:13px; font-weight:800;
    }}
    .signal-card p, .poster-copy p, .editorial-copy p {{
      margin:14px 0 0; color:var(--ink-soft); font-size:14px; line-height:1.6;
    }}
    .poster-card {{
      display:grid; grid-template-columns:170px 1fr; gap:16px; align-items:center;
      background:linear-gradient(135deg, rgba(20,57,43,.96), rgba(35,84,60,.92));
      color:#f7f4ea;
    }}
    .poster-card .mini-label {{ color:#f6d998; }}
    .poster-copy p {{ color:rgba(247,244,234,.78); }}
    .poster-visual {{
      width:170px; height:220px; border-radius:22px; object-fit:cover; display:block;
      border:1px solid rgba(255,255,255,.12); background:rgba(255,255,255,.08);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.08);
    }}
    .poster-visual-fallback {{
      display:flex; align-items:center; justify-content:center; text-align:center; padding:18px; font-weight:800; line-height:1.55;
    }}
    .editorial-card {{
      display:grid; grid-template-columns:140px 1fr; gap:16px; align-items:center; padding:16px;
    }}
    .editorial-thumb {{
      width:140px; height:140px; object-fit:cover; border-radius:20px; display:block; background:var(--forest-deep);
    }}
    .editorial-thumb-fallback {{
      display:flex; align-items:center; justify-content:center; color:#f8ebd0; font-weight:800; letter-spacing:.08em;
    }}
    .section-block {{
      margin-top:24px; padding:28px; border-radius:var(--radius-xl); border:1px solid rgba(255,255,255,.44);
      background:var(--paper); box-shadow: 0 26px 62px rgba(19, 39, 30, 0.10); backdrop-filter: blur(14px);
      position:relative; overflow:hidden;
    }}
    .section-block::before {{
      content:""; position:absolute; inset:0; pointer-events:none; opacity:.42;
      background:
        radial-gradient(circle at 10% 18%, rgba(200,154,43,.12), transparent 18%),
        radial-gradient(circle at 90% 16%, rgba(30,91,67,.10), transparent 16%),
        linear-gradient(180deg, transparent 78%, rgba(30,91,67,.08) 78.4%, rgba(30,91,67,.08) 78.8%, transparent 79.2%);
    }}
    .section-head {{
      display:grid; grid-template-columns:minmax(0, 1fr) minmax(300px, 440px); align-items:end; gap:18px; margin-bottom:20px; position:relative; z-index:1;
    }}
    .section-head h2 {{
      margin:10px 0 0; font-family:"Bricolage Grotesque", sans-serif; font-size:52px; line-height:.92; letter-spacing:-.04em;
    }}
    .section-head p {{ max-width:560px; margin:0; color:var(--ink-soft); font-size:15px; line-height:1.6; }}
    .offer-grid {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:16px; }}
    .offer-card {{
      min-height:220px; padding:22px; border-radius:24px; border:1px solid var(--line);
      background:linear-gradient(180deg, rgba(255,255,255,.88), rgba(255,250,241,.92));
      position:relative; overflow:hidden; display:flex; flex-direction:column;
      transition:transform .2s ease, border-color .2s ease, box-shadow .2s ease;
    }}
    .offer-card::after {{
      content:""; position:absolute; inset:auto -40px -60px auto; width:140px; height:140px; border-radius:50%;
      background:radial-gradient(circle, rgba(30,91,67,.10), transparent 70%);
      opacity:.9;
    }}
    .offer-card:hover {{ transform:translateY(-4px); border-color:var(--line-strong); box-shadow:0 18px 34px rgba(20,57,43,.10); }}
    .offer-index {{
      width:46px; height:46px; display:grid; place-items:center; border-radius:16px;
      font-family:"Bricolage Grotesque", sans-serif; font-size:22px; font-weight:800;
      background:linear-gradient(135deg, rgba(30,91,67,.14), rgba(200,154,43,.20)); color:var(--forest);
    }}
    .offer-card strong {{ display:block; margin-top:14px; font-size:20px; }}
    .offer-card p {{ margin:10px 0 0; color:var(--ink-soft); font-size:14px; line-height:1.62; }}
    .preview-grid {{ display:grid; grid-template-columns:minmax(0, 1.16fr) minmax(320px, .84fr); gap:18px; align-items:stretch; }}
    .landing-stack, .landing-news-grid {{ display:grid; gap:14px; align-content:start; }}
    .preview-side {{ display:grid; gap:16px; align-content:start; }}
    .preview-side-block {{
      height:100%; padding:18px; border-radius:26px; border:1px solid var(--line);
      background:linear-gradient(180deg, rgba(255,255,255,.88), rgba(255,249,239,.92));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.64);
      display:flex; flex-direction:column; gap:16px; position:relative; overflow:hidden;
    }}
    .preview-side-block::before,
    .architecture-card::before,
    .collection-card::before {{
      content:""; position:absolute; inset:0 auto auto 0; width:100%; height:4px;
      background:linear-gradient(90deg, var(--forest), rgba(200,154,43,.58), transparent);
      opacity:.9;
    }}
    .preview-side-head {{
      display:flex; align-items:flex-start; justify-content:space-between; gap:14px;
    }}
    .preview-side-head span {{
      display:block; color:#8b6220; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.08em;
    }}
    .preview-side-head strong {{
      display:block; margin-top:6px; font-family:"Bricolage Grotesque", sans-serif; font-size:26px; line-height:1; letter-spacing:-.03em;
    }}
    .preview-side-head a {{
      min-height:38px; padding:0 14px; border-radius:999px; display:inline-flex; align-items:center; justify-content:center;
      text-decoration:none; color:var(--forest); font-size:13px; font-weight:800; border:1px solid rgba(16,40,31,.10);
      background:rgba(255,255,255,.72);
    }}
    .landing-pick-card {{
      min-height:238px; padding:18px; border-radius:24px; border:1px solid var(--line);
      background:linear-gradient(180deg, rgba(255,255,255,.92), rgba(255,252,246,.92));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.68);
      position:relative; overflow:hidden; display:flex; flex-direction:column;
      transition:transform .22s ease, border-color .22s ease, box-shadow .22s ease;
    }}
    .landing-pick-card::before {{
      content:""; position:absolute; inset:auto 0 0 0; height:54px;
      background:linear-gradient(180deg, transparent, rgba(30,91,67,.08));
      pointer-events:none;
    }}
    .landing-pick-card.pick-home {{ background:linear-gradient(135deg, rgba(227,246,238,.95), rgba(255,252,246,.95)); }}
    .landing-pick-card.pick-away {{ background:linear-gradient(135deg, rgba(230,238,252,.95), rgba(255,252,246,.95)); }}
    .landing-pick-card.pick-draw {{ background:linear-gradient(135deg, rgba(252,243,216,.95), rgba(255,252,246,.95)); }}
    .landing-pick-card:hover {{ transform:translateY(-4px); border-color:var(--line-strong); box-shadow:0 18px 34px rgba(20,57,43,.10); }}
    .landing-pick-top {{ display:flex; align-items:flex-start; justify-content:space-between; gap:14px; }}
    .landing-kickoff {{ color:#89611c; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.08em; }}
    .landing-pick-card h3 {{ margin:10px 0 8px; font-family:"Bricolage Grotesque", sans-serif; font-size:26px; line-height:1.05; letter-spacing:-.03em; }}
    .landing-pick-card h3 span {{ font-size:17px; font-weight:700; color:var(--ink-soft); }}
    .landing-league {{ color:var(--ink-soft); font-size:14px; }}
    .pick-badge {{
      min-width:84px; padding:8px 10px; border-radius:18px; text-align:center;
      background:rgba(255,255,255,.78); border:1px solid rgba(16,40,31,.10);
    }}
    .pick-value {{
      display:block; font-family:"Bricolage Grotesque", sans-serif; font-size:28px; font-weight:800; color:var(--forest); line-height:1;
    }}
    .pick-percent {{ display:block; margin-top:5px; color:var(--ink-soft); font-size:12px; font-weight:800; }}
    .landing-pick-card p {{ margin:14px 0 0; color:var(--ink-soft); font-size:14px; line-height:1.6; flex:1 1 auto; }}
    .landing-pick-meta {{
      display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:auto; padding-top:14px;
      border-top:1px solid rgba(16,40,31,.08); color:var(--ink-soft); font-size:13px;
    }}
    .landing-pick-meta a {{ text-decoration:none; color:var(--forest); font-weight:800; }}
    .landing-article-card {{
      display:grid; grid-template-columns:148px minmax(0, 1fr); gap:14px; padding:14px; border-radius:24px; border:1px solid var(--line);
      background:linear-gradient(180deg, rgba(255,255,255,.88), rgba(255,251,244,.92));
      min-height:176px; align-items:stretch;
      transition:transform .22s ease, border-color .22s ease, box-shadow .22s ease;
    }}
    .landing-article-card:hover {{ transform:translateY(-4px); border-color:var(--line-strong); box-shadow:0 18px 34px rgba(20,57,43,.10); }}
    .landing-article-image {{
      width:148px; height:148px; object-fit:cover; border-radius:20px; display:block; background:var(--forest-deep);
    }}
    .landing-article-fallback {{ display:flex; align-items:center; justify-content:center; color:#f7ead3; font-weight:800; letter-spacing:.08em; }}
    .landing-article-source {{
      color:#8b6220; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.08em;
    }}
    .landing-article-copy h3 {{
      margin:8px 0; font-family:"Bricolage Grotesque", sans-serif; font-size:20px; line-height:1.18; letter-spacing:-.02em;
    }}
    .landing-article-copy {{ display:flex; flex-direction:column; min-width:0; }}
    .landing-article-copy p {{ margin:0; color:var(--ink-soft); font-size:14px; line-height:1.55; }}
    .landing-article-bottom {{
      display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:auto; padding-top:12px; color:var(--ink-soft); font-size:13px;
    }}
    .landing-article-link {{ text-decoration:none; color:var(--forest); font-weight:800; }}
    .architecture-grid {{ display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:16px; }}
    .architecture-card {{
      min-height:220px; padding:22px; border-radius:26px; border:1px solid var(--line);
      background:
        radial-gradient(circle at top right, rgba(200,154,43,.16), transparent 34%),
        linear-gradient(180deg, rgba(255,255,255,.92), rgba(250,244,233,.96));
      position:relative; overflow:hidden; display:flex; flex-direction:column; gap:14px;
      box-shadow: 0 18px 38px rgba(20,57,43,.08);
    }}
    .architecture-card::after {{
      content:""; position:absolute; inset:auto -26px -34px auto; width:118px; height:118px; border-radius:50%;
      background:radial-gradient(circle, rgba(30,91,67,.10), transparent 68%);
    }}
    .architecture-step {{
      width:50px; height:50px; border-radius:18px; display:grid; place-items:center;
      font-family:"Bricolage Grotesque", sans-serif; font-size:22px; font-weight:800; color:var(--forest);
      background:linear-gradient(135deg, rgba(30,91,67,.14), rgba(200,154,43,.22));
    }}
    .architecture-card strong {{
      display:block; font-family:"Bricolage Grotesque", sans-serif; font-size:28px; line-height:1.02; letter-spacing:-.03em;
    }}
    .architecture-card p {{ margin:0; color:var(--ink-soft); font-size:14px; line-height:1.62; flex:1 1 auto; }}
    .architecture-link {{
      text-decoration:none; color:var(--forest); font-weight:800; position:relative; z-index:1;
    }}
    .collection-grid {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:16px; }}
    .collection-card {{
      min-height:220px; padding:22px; border-radius:26px; text-decoration:none; color:var(--ink); border:1px solid var(--line);
      background:
        radial-gradient(circle at top right, rgba(200,154,43,.18), transparent 36%),
        radial-gradient(circle at bottom left, rgba(30,91,67,.10), transparent 28%),
        linear-gradient(180deg, rgba(255,255,255,.92), rgba(250,244,233,.96));
      transition:transform .18s ease, box-shadow .18s ease, border-color .18s ease;
      position:relative; overflow:hidden; display:flex; flex-direction:column;
    }}
    .collection-card::after {{
      content:""; position:absolute; left:-14px; bottom:-14px; width:92px; height:92px; border-radius:50%;
      border:1px solid rgba(30,91,67,.10);
      opacity:.8;
    }}
    .collection-card:hover {{ transform:translateY(-3px); border-color:var(--line-strong); box-shadow: var(--shadow); }}
    .collection-card span {{
      color:#8b6220; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.08em;
    }}
    .collection-card strong {{
      display:block; margin-top:14px; font-family:"Bricolage Grotesque", sans-serif; font-size:28px; line-height:1.02; letter-spacing:-.03em;
    }}
    .collection-card p {{ margin:12px 0 0; color:var(--ink-soft); font-size:14px; line-height:1.6; flex:1 1 auto; }}
    .site-footer {{
      margin-top:20px; padding:22px 24px; border-radius:28px; border:1px solid rgba(255,255,255,.48);
      background:rgba(255,250,242,.70); display:grid; grid-template-columns:minmax(0, 1.1fr) minmax(260px, .9fr); gap:18px; align-items:start;
    }}
    .site-footer h3 {{
      margin:0; font-family:"Bricolage Grotesque", sans-serif; font-size:34px; line-height:1; letter-spacing:-.03em;
    }}
    .site-footer p, .site-footer a {{
      color:var(--ink-soft); font-size:14px; line-height:1.65; text-decoration:none;
    }}
    .site-footer-links {{ display:grid; gap:8px; justify-items:start; }}
    .site-footer-links a {{
      min-height:40px; padding:0 14px; border-radius:999px; display:inline-flex; align-items:center;
      background:rgba(255,255,255,.52); border:1px solid rgba(16,40,31,.08);
    }}
    .landing-empty {{
      padding:18px; border-radius:18px; border:1px dashed rgba(16,40,31,.16);
      color:var(--ink-soft); background:rgba(255,255,255,.46); font-size:14px; line-height:1.6;
    }}
    .reveal {{
      opacity:0; transform:translateY(24px) scale(.985);
      transition:
        opacity .72s ease,
        transform .72s cubic-bezier(.2,.8,.2,1);
      transition-delay: var(--reveal-delay, 0ms);
    }}
    .reveal.in-view {{
      opacity:1; transform:translateY(0) scale(1);
    }}
    @keyframes driftGlow {{
      0%, 100% {{ transform: translate3d(0, 0, 0) scale(1); opacity:.72; }}
      50% {{ transform: translate3d(-20px, -16px, 0) scale(1.05); opacity:1; }}
    }}
    @keyframes floatBall {{
      0%, 100% {{ transform: translate3d(0, 0, 0) rotate(0deg); }}
      50% {{ transform: translate3d(-10px, 10px, 0) rotate(9deg); }}
    }}
    @keyframes pulseGlow {{
      0%, 100% {{ transform: scale(1); opacity:.72; }}
      50% {{ transform: scale(1.08); opacity:1; }}
    }}
    @media (max-width: 1140px) {{
      .hero-stage {{ grid-template-columns: 1fr; }}
      .section-head {{ grid-template-columns:1fr; }}
      .offer-grid, .architecture-grid, .collection-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .preview-grid {{ grid-template-columns: 1fr; }}
      .preview-side {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .hero-facts {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 760px) {{
      .site-shell {{ padding:16px 14px 32px; }}
      .site-topbar {{ align-items:flex-start; flex-direction:column; }}
      .site-nav {{ width:100%; justify-content:flex-start; }}
      .hero-stage {{ padding:18px; }}
      .hero-ball {{ width:54px; height:54px; right:16px; top:18px; }}
      .hero-copy h1 {{ font-size: clamp(3rem, 15vw, 4.8rem); }}
      .preview-grid, .preview-side, .hero-facts, .hero-rail, .offer-grid, .architecture-grid, .collection-grid {{ grid-template-columns:1fr; }}
      .poster-card, .editorial-card, .landing-article-card, .site-footer {{ grid-template-columns:1fr; }}
      .poster-visual, .editorial-thumb, .landing-article-image {{ width:100%; height:220px; }}
      .section-head {{ grid-template-columns:1fr; }}
      .section-head h2 {{ font-size:42px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .hero-stage::before, .hero-ball, .hero-glow {{ animation:none; }}
      html {{ scroll-behavior:auto; }}
    }}
  </style>
</head>
<body>
  <div class="site-shell">
    <div class="site-topbar">
      <a class="site-brand" href="/">
        <span class="site-brand-mark">GF</span>
        <span class="site-brand-copy">
          <strong>GABFOOT</strong>
          <span>Studio premium pour lire les matchs, pas juste empiler des pronostics.</span>
        </span>
      </a>
      <nav class="site-nav">
        <a href="#signature">Signature</a>
        <a href="#live-preview">Top picks</a>
        <a href="#site-map">Architecture</a>
        <a href="#entry-points">Parcours</a>
        <a class="site-nav-cta" href="/dashboard?limit={limit}&min_percent={min_percent}">Ouvrir le dashboard</a>
      </nav>
    </div>

    <section class="hero-stage">
      <div class="hero-decor">
        <div class="hero-ball"></div>
        <div class="hero-glow"></div>
      </div>
      <div class="hero-copy">
        <span class="eyebrow">Football signal studio</span>
        <h1>Des pronostics foot plus propres, plus premium et plus lisibles.</h1>
        <p>GABFOOT prend l&apos;esprit d&apos;un gros site de pronostics et le rend plus editorial, plus clair et plus credible. Les matchs forts, la Botola, les news clubs et le poster Telegram vivent dans une meme direction visuelle, avec une palette pensee pour inspirer confiance.</p>
        <div class="hero-actions">
          <a class="site-btn" href="/dashboard?limit={limit}&min_percent={min_percent}">Entrer dans le dashboard</a>
          <a class="ghost-site-btn" href="/articles">Voir les news</a>
          <a class="ghost-site-btn" href="/updates">Voir les nouveautes</a>
          <a class="ghost-site-btn" href="/botola">Explorer la Botola</a>
        </div>
        <div class="hero-rail">
          <div class="hero-rail-item">
            <strong>Board principal</strong>
            <span>Les picks les plus solides remontent avant le reste.</span>
          </div>
          <div class="hero-rail-item">
            <strong>Radar editorial</strong>
            <span>Le flux news complete le signal au lieu de le parasiter.</span>
          </div>
          <div class="hero-rail-item">
            <strong>Sortie Telegram</strong>
            <span>Le site produit aussi une image prete a publier.</span>
          </div>
        </div>
        <div class="hero-facts">
          <div class="hero-fact">
            <strong>{len(matches)}</strong>
            <span>signals majeurs actifs avec ton filtre a {min_percent}%.</span>
          </div>
          <div class="hero-fact">
            <strong>{avg_confidence}%</strong>
            <span>fiabilite moyenne lue en une seconde.</span>
          </div>
          <div class="hero-fact">
            <strong>{len(botola) + len(tennis)}</strong>
            <span>boards additionnels entre Maroc et tennis.</span>
          </div>
          <div class="hero-fact">
            <strong>{len(articles)}</strong>
            <span>angles editoriaux pour habiller le produit.</span>
          </div>
          <div class="hero-fact">
            <strong>{len(site_updates)}</strong>
            <span>mises a jour produit deja publiees.</span>
          </div>
        </div>
        <div class="market-strip">
          {market_strip}
        </div>
        {notice_html}
      </div>

      <div class="hero-side">
        {featured_match_html}
        <article class="hero-card poster-card">
          {poster_visual}
          <div class="poster-copy">
            <div class="mini-label">Telegram studio</div>
            <h3>Une affiche prete a diffuser sans sortir de l&apos;univers GABFOOT.</h3>
            <p>Le site ne se limite pas a l&apos;inspiration visuelle. Il pousse aussi une image exploitable pour les canaux Telegram, avec la meme logique de selection que le dashboard.</p>
            <div class="hero-actions" style="margin-top:14px;">
              <a class="outline-btn" href="/image" target="_blank">Voir le poster</a>
              <a class="outline-btn" href="/dashboard?limit={limit}&min_percent={min_percent}#top-picks">Voir les picks</a>
            </div>
          </div>
        </article>
        {featured_article_html}
      </div>
    </section>

    <section id="signature" class="section-block reveal">
      <div class="section-head">
        <div>
          <div class="mini-label">Direction du produit</div>
          <h2>Une signature plus forte que le simple tableau de pronostics.</h2>
        </div>
        <p>Le projet garde la richesse d&apos;un site de pronostics populaire, mais remonte en gamme avec une meilleure hierarchie, une couleur plus maitrisee et une lecture plus rapide des signaux utiles.</p>
      </div>
      <div class="offer-grid">
        <article class="offer-card reveal">
          <div class="offer-index">01</div>
          <strong>Un ton plus credible</strong>
          <p>Fond sable chaud, vert profond et accent or. Le rendu evoque l&apos;analyse et la confiance, pas le casino agressif ni la page surchargee.</p>
        </article>
        <article class="offer-card reveal">
          <div class="offer-index">02</div>
          <strong>Une lecture plus rapide</strong>
          <p>Les picks importants remontent tout de suite. En quelques secondes, tu vois les matchs forts, le niveau de fiabilite et les boards annexes qui comptent.</p>
        </article>
        <article class="offer-card reveal">
          <div class="offer-index">03</div>
          <strong>Un produit plus complet</strong>
          <p>Dashboard, fiches match, favoris personnels, flux editorial et poster Telegram travaillent ensemble au lieu de donner l&apos;impression de blocs isoles.</p>
        </article>
      </div>
    </section>

    <section id="live-preview" class="section-block reveal">
      <div class="section-head">
        <div>
          <div class="mini-label">Lecture instantanee</div>
          <h2>Le niveau du flux actuel, sans ouvrir dix pages.</h2>
        </div>
        <p>La home montre deja le ton du produit. Les meilleures cartes doivent suffire a convaincre que GABFOOT sait trier, cadrer et raconter les matchs qui meritent l&apos;attention.</p>
      </div>
      <div class="preview-grid">
        <div class="landing-stack">
          {render_landing_pick_cards(matches)}
        </div>
        <div class="preview-side">
          <section class="preview-side-block reveal">
            <div class="preview-side-head">
              <div>
                <span>Editorial</span>
                <strong>Clubs & joueurs</strong>
              </div>
              <a href="/articles">Voir tout</a>
            </div>
            <div class="landing-news-grid">
              {render_landing_article_teasers(articles)}
            </div>
          </section>
          <section class="preview-side-block reveal">
            <div class="preview-side-head">
              <div>
                <span>Produit</span>
                <strong>Nouveautes</strong>
              </div>
              <a href="/updates">Voir tout</a>
            </div>
            <div class="landing-news-grid">
              {render_landing_update_teasers(site_updates)}
            </div>
          </section>
        </div>
      </div>
    </section>

    <section id="site-map" class="section-block reveal">
      <div class="section-head">
        <div>
          <div class="mini-label">Architecture du site</div>
          <h2>Une colonne centrale claire, des branches utiles, aucun bloc perdu.</h2>
        </div>
        <p>La home doit expliquer tout le produit. Le dashboard porte l&apos;execution, les boards annexes elargissent la couverture, l&apos;editorial habille le site et le journal produit montre que la plateforme avance.</p>
      </div>
      <div class="architecture-grid">
        <article class="architecture-card reveal">
          <div class="architecture-step">01</div>
          <strong>Accueil</strong>
          <p>La page d&apos;entree pose le ton, presente le signal fort et oriente immediatement vers les axes qui comptent.</p>
          <a class="architecture-link" href="/">Rester sur la vitrine</a>
        </article>
        <article class="architecture-card reveal">
          <div class="architecture-step">02</div>
          <strong>Dashboard</strong>
          <p>Le coeur operationnel ou tu filtres les matchs, pilotes le seuil de confiance et pousses le contenu exploitable.</p>
          <a class="architecture-link" href="/dashboard?limit={limit}&min_percent={min_percent}">Ouvrir le board principal</a>
        </article>
        <article class="architecture-card reveal">
          <div class="architecture-step">03</div>
          <strong>Boards</strong>
          <p>Botola et Tennis etendent la lecture sans casser la structure. Chaque univers garde sa place au lieu d&apos;etre ajoute au hasard.</p>
          <a class="architecture-link" href="/botola">Voir les boards secondaires</a>
        </article>
        <article class="architecture-card reveal">
          <div class="architecture-step">04</div>
          <strong>Editorial</strong>
          <p>Articles et nouveautes donnent du contexte, de la credibilite et une preuve visible que le site est vivant.</p>
          <a class="architecture-link" href="/articles">Lire le flux editorial</a>
        </article>
      </div>
    </section>

    <section id="entry-points" class="section-block reveal">
      <div class="section-head">
        <div>
          <div class="mini-label">Parcours rapides</div>
          <h2>Des parcours alignes avec la logique du produit.</h2>
        </div>
        <p>Chaque bloc a maintenant une fonction nette dans l&apos;architecture globale. Les liens ne se battent plus entre eux et gardent la meme hierarchie visuelle.</p>
      </div>
      <div class="collection-grid">
        <a class="collection-card reveal" href="/dashboard?limit={limit}&min_percent={min_percent}">
          <span>Control room</span>
          <strong>Dashboard premium</strong>
          <p>La vue operationnelle pour les picks classes, les favoris embarques, les liens match et l&apos;affiche Telegram.</p>
        </a>
        <a class="collection-card reveal" href="/botola">
          <span>Maroc</span>
          <strong>Botola Pro</strong>
          <p>Une porte locale lisible, pensee pour donner au projet une vraie couleur marocaine et une entree editorialement forte.</p>
        </a>
        <a class="collection-card reveal" href="/tennis">
          <span>Multi-board</span>
          <strong>Tennis World</strong>
          <p>Un second board pour montrer que GABFOOT peut aussi traiter d&apos;autres flux sans casser son identite visuelle.</p>
        </a>
        <a class="collection-card reveal" href="/articles">
          <span>Editorial</span>
          <strong>Clubs & joueurs</strong>
          <p>Le contenu qui donne du relief a la page, nourrit le partage et rapproche la plateforme d&apos;un vrai media produit.</p>
        </a>
        <a class="collection-card reveal" href="/updates">
          <span>Produit</span>
          <strong>Nouveautes & updates</strong>
          <p>Le journal des ameliorations du site pour montrer que GABFOOT evolue, s'enrichit et garde un rythme de publication visible.</p>
        </a>
      </div>
    </section>

    <footer class="site-footer reveal">
      <div>
        <h3>GABFOOT</h3>
        <p>La page d&apos;accueil pose maintenant une vraie identite: moins chargee, plus premium, plus nette. Le coeur operationnel reste accessible sur <a href="/dashboard">/dashboard</a> pour piloter les picks, Telegram et les boards annexes.</p>
      </div>
      <div class="site-footer-links">
        <a href="/dashboard?limit={limit}&min_percent={min_percent}">Ouvrir le dashboard</a>
        <a href="/image" target="_blank">Voir le poster</a>
        <a href="/botola">Pronostics Botola</a>
        <a href="/articles">Articles football</a>
        <a href="/updates">Nouveautes produit</a>
      </div>
    </footer>
  </div>
</body>
{interaction_script()}
</html>"""


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
          <a href="/dashboard">Dashboard</a>
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
          <a href="/dashboard">Dashboard</a>
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
        <a href="/dashboard">Dashboard</a>
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
    site_url: str = "",
) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    botola = botola or []
    tennis = tennis or []
    articles = articles or []
    site_url = site_url.rstrip("/")
    canonical_url = f"{site_url}/dashboard" if site_url else ""
    icon_url = absolute_url(site_url, "/icon.png") if site_url else "/icon.png"
    canonical_tag = f'<link rel="canonical" href="{html.escape(canonical_url)}">' if canonical_url else ""
    og_url_tag = f'<meta property="og:url" content="{html.escape(canonical_url)}">' if canonical_url else ""
    featured_match = matches[0] if matches else None
    featured_article = articles[0] if articles else None
    avg_confidence = int(round(sum(match.sureness_percent for match in matches) / len(matches))) if matches else 0
    prediction_table = render_prediction_table(matches)
    botola_table = render_botola_table(botola)
    tennis_table = render_tennis_table(tennis)
    article_cards = render_articles_grid(articles)
    market_strip = render_market_strip(matches, botola)
    if DASHBOARD_HERO_IMAGE_PATH.exists():
        dashboard_hero_visual = f"""
        <div class="hero-media">
          <img class="hero-media-image" src="/dashboard-hero.jpg" alt="Visuel premium GABFOOT pour le dashboard">
          <div class="hero-media-overlay">
            <div class="hero-media-tag">Visual studio</div>
            <div class="hero-badge">
              <strong>{avg_confidence}%</strong>
              <span>Fiabilite moyenne du board actif avec un filtre fixe a {min_percent}% minimum.</span>
            </div>
          </div>
        </div>
        """
    else:
        dashboard_hero_visual = f"""
        <div class="hero-badge">
          <strong>{avg_confidence}%</strong>
          <span>Fiabilite moyenne du board actif avec un filtre fixe a {min_percent}% minimum.</span>
        </div>
        """

    image_block = ""
    if card_path and card_path.exists():
        image_block = """
        <section class="panel image-panel reveal">
          <div class="panel-head">
            <div class="panel-title">
              <span class="panel-pill">Poster</span>
              <h2>Affiche Telegram</h2>
            </div>
            <a class="ghost-btn light-ghost-btn" href="/image" target="_blank">Ouvrir l'image</a>
          </div>
          <p class="section-note">Une sortie visuelle prete a diffuser, construite avec le meme filtre que le board principal.</p>
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
        <section class="studio-card reveal">
          <div class="spotlight-label">Match signature</div>
          <h2>{html.escape(featured_match.home_name)} <span>vs</span> {html.escape(featured_match.away_name)}</h2>
          <div class="spotlight-meta">{html.escape(featured_match.tournament_name)} • {html.escape(fmt_kickoff(featured_match.kickoff_utc))}</div>
          <div class="spotlight-badges">
            <span>{html.escape(featured_match.prediction)} • {featured_match.sureness_percent}%</span>
            <span>{html.escape(featured_match.home_form)} | {html.escape(featured_match.away_form)}</span>
            <span>H2H {html.escape(str(featured_match.h2h_edge))}</span>
          </div>
          <p>{html.escape(' • '.join(featured_match.why))}</p>
          <div class="studio-actions"><a class="btn" href="/match/{featured_match.match_id}">Voir la fiche match</a>{featured_favorite}</div>
        </section>
        """
    editorial_panel = ""
    if featured_article:
        image = str(featured_article.get("image", "")).strip()
        visual = f'<img class="editorial-image" src="{html.escape(image)}" alt="{html.escape(featured_article["team"])}">' if image else '<div class="editorial-image editorial-fallback">NEWS</div>'
        article_link = f'<a class="btn light-btn" href="{html.escape(featured_article["url"])}" target="_blank" rel="noreferrer">Lire l&apos;analyse</a>' if featured_article.get("url") else ""
        editorial_panel = f"""
        <section class="editorial-card reveal">
          <div class="editorial-visual">{visual}</div>
          <div class="editorial-copy">
            <div class="spotlight-label">Radar editorial</div>
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
          <span class="panel-pill">Mon board</span>
          <h2>Mes favoris</h2>
        </div>
        <span class="footer">Stockes localement sur cet appareil</span>
      </div>
      <p class="section-note">Les matchs enregistres ici servent de shortlist personnelle. Tu peux construire ton propre board sans casser le flux principal.</p>
      <div class="favorites-board">
        <div id="favorites-list" class="favorites-grid"></div>
        <div id="favorites-empty" class="favorites-empty">Ajoute des matchs depuis les tableaux ou les fiches pour garder tes meilleurs spots dans un espace a part.</div>
      </div>
    </section>
    """
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GABFOOT | Control Room Pronostics Foot</title>
  <meta name="description" content="Le dashboard GABFOOT classe les picks les plus solides, les signaux Botola, le tennis et les articles football dans une interface plus premium et plus lisible.">
  <meta property="og:title" content="GABFOOT | Control Room Pronostics Foot">
  <meta property="og:description" content="Dashboard GABFOOT avec picks premium, flux editorial, Botola et poster Telegram dans un univers plus haut de gamme.">
  <meta property="og:type" content="website">
  <meta property="og:image" content="{html.escape(icon_url)}">
  {og_url_tag}
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="GABFOOT | Control Room Pronostics Foot">
  <meta name="twitter:description" content="Une salle de controle GABFOOT plus premium, plus claire et plus credible pour les pronostics foot.">
  <meta name="twitter:image" content="{html.escape(icon_url)}">
  <meta name="theme-color" content="#14382c">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  {canonical_tag}
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="icon" type="image/png" href="/icon.png">
  <link rel="apple-touch-icon" href="/icon.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@400;500;700;800&family=Source+Sans+3:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --sand: #f4efe2;
      --sand-2: #e8dcc4;
      --surface: rgba(255, 252, 244, 0.94);
      --surface-strong: rgba(250, 244, 231, 0.98);
      --forest: #14382c;
      --forest-deep: #0d221a;
      --forest-soft: #315447;
      --ink: #13211c;
      --muted: #66756c;
      --line: rgba(19, 33, 28, 0.10);
      --line-strong: rgba(20, 56, 44, 0.20);
      --gold: #bf8f2f;
      --gold-soft: #e1bc67;
      --gold-pale: rgba(191, 143, 47, 0.12);
      --blue-soft: #7eabc8;
      --danger: #b85a39;
      --shadow: 0 24px 60px rgba(19, 33, 28, 0.12);
      --shadow-soft: 0 18px 42px rgba(19, 33, 28, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Source Sans 3", "DejaVu Sans", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(191,143,47,.14), transparent 20%),
        radial-gradient(circle at 92% 8%, rgba(20,56,44,.16), transparent 22%),
        linear-gradient(180deg, #f8f2e8 0%, #f2eadc 52%, #ece1cf 100%);
    }}
    a {{ color: inherit; }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 24px 18px 56px; }}
    .hero {{
      position: relative;
      overflow: hidden;
      padding: 24px;
      border-radius: 34px;
      background:
        radial-gradient(circle at top right, rgba(225,188,103,.22), transparent 28%),
        radial-gradient(circle at bottom left, rgba(255,255,255,.08), transparent 26%),
        linear-gradient(135deg, rgba(13,34,26,.97), rgba(27,66,51,.94));
      color: #f8f3e8;
      border: 1px solid rgba(255,255,255,.10);
      box-shadow: 0 32px 70px rgba(13, 34, 26, 0.22);
    }}
    .hero::before {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(120deg, rgba(255,255,255,.04), transparent 36%),
        radial-gradient(circle at 12% 18%, rgba(225,188,103,.18), transparent 22%);
    }}
    .hero::after {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      opacity: .34;
      background:
        radial-gradient(circle at 50% 102%, rgba(225,188,103,.24), transparent 24%),
        linear-gradient(90deg, transparent 14%, rgba(248,243,232,.08) 14.4%, rgba(248,243,232,.08) 14.8%, transparent 15.2%, transparent 84.8%, rgba(248,243,232,.08) 85.2%, rgba(248,243,232,.08) 85.6%, transparent 86%),
        linear-gradient(transparent 78%, rgba(248,243,232,.08) 78.4%, rgba(248,243,232,.08) 78.8%, transparent 79.2%);
    }}
    .hero-top,
    .hero-overview,
    .hero-strip {{
      position: relative;
      z-index: 1;
    }}
    .hero-top {{
      display: grid;
      grid-template-columns: minmax(0, 1.04fr) minmax(320px, .96fr);
      gap: 18px;
      align-items: start;
    }}
    .hero-quickstrip {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }}
    .hero-quickstrip span {{
      min-height: 36px;
      padding: 0 13px;
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      background: rgba(255,255,255,.07);
      border: 1px solid rgba(255,255,255,.10);
      color: rgba(248,243,232,.88);
      font-size: 13px;
      font-weight: 700;
    }}
    .hero-kicker {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 0 14px;
      border-radius: 999px;
      background: rgba(225,188,103,.14);
      border: 1px solid rgba(225,188,103,.22);
      color: #f4d489;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    .hero-media {{
      position: relative;
      min-height: 240px;
      border-radius: 30px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,.12);
      box-shadow: 0 28px 50px rgba(6, 18, 13, .24), inset 0 1px 0 rgba(255,255,255,.04);
      background:
        linear-gradient(180deg, rgba(7,17,12,.18), rgba(7,17,12,.58)),
        radial-gradient(circle at 18% 18%, rgba(225,188,103,.24), transparent 24%);
    }}
    .hero-media::after {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(180deg, rgba(0,0,0,.04), rgba(0,0,0,.42)),
        linear-gradient(90deg, transparent 12%, rgba(248,243,232,.10) 12.5%, rgba(248,243,232,.10) 12.9%, transparent 13.3%, transparent 86.7%, rgba(248,243,232,.10) 87.1%, rgba(248,243,232,.10) 87.5%, transparent 88%);
    }}
    .hero-media-image {{
      width: 100%;
      height: 100%;
      min-height: 240px;
      object-fit: cover;
      display: block;
    }}
    .hero-media-overlay {{
      position: absolute;
      inset: 0;
      display: flex;
      flex-direction: column;
      align-items: end;
      justify-content: space-between;
      padding: 18px;
    }}
    .hero-media-tag {{
      min-height: 34px;
      padding: 0 12px;
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      background: rgba(7,17,12,.54);
      border: 1px solid rgba(255,255,255,.10);
      color: rgba(248,243,232,.88);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .10em;
      text-transform: uppercase;
      backdrop-filter: blur(8px);
    }}
    .hero-badge {{
      display: grid;
      gap: 2px;
      min-width: 170px;
      padding: 16px 18px;
      border-radius: 22px;
      background: rgba(7,17,12,.56);
      border: 1px solid rgba(255,255,255,.12);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.06), 0 16px 32px rgba(0,0,0,.18);
      backdrop-filter: blur(10px);
    }}
    .hero-badge strong {{
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 34px;
      line-height: 1;
      letter-spacing: -.04em;
    }}
    .hero-badge span {{
      color: rgba(248,243,232,.78);
      font-size: 13px;
      line-height: 1.45;
    }}
    h1 {{
      margin: 14px 0 0;
      max-width: 760px;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 58px;
      line-height: .94;
      letter-spacing: -.05em;
    }}
    .sub {{
      margin-top: 14px;
      max-width: 760px;
      color: rgba(248,243,232,.82);
      font-size: 18px;
      line-height: 1.58;
    }}
    .nav-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 20px;
    }}
    .nav-link {{
      display: inline-flex;
      align-items: center;
      min-height: 40px;
      padding: 0 14px;
      border-radius: 999px;
      text-decoration: none;
      color: #f8f3e8;
      border: 1px solid rgba(255,255,255,.12);
      background: rgba(255,255,255,.05);
      font-size: 14px;
      font-weight: 700;
      transition: transform .18s ease, background .18s ease, border-color .18s ease;
    }}
    .nav-link:hover,
    .nav-link:active {{
      transform: translateY(-1px);
      background: rgba(225,188,103,.14);
      border-color: rgba(225,188,103,.26);
    }}
    .hero-overview {{
      display: grid;
      grid-template-columns: 1.06fr .94fr;
      gap: 18px;
      margin-top: 18px;
      align-items: stretch;
    }}
    .hero-stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .hero-stat {{
      padding: 16px 16px 15px;
      border-radius: 22px;
      border: 1px solid rgba(255,255,255,.10);
      background: linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.04));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.05);
    }}
    .hero-stat-label {{
      color: rgba(248,243,232,.64);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .10em;
    }}
    .hero-stat-value {{
      margin-top: 10px;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 34px;
      line-height: 1;
      letter-spacing: -.04em;
    }}
    .hero-stat-note {{
      margin-top: 8px;
      color: rgba(248,243,232,.76);
      font-size: 14px;
      line-height: 1.45;
    }}
    .market-strip {{
      display: flex;
      gap: 10px;
      overflow-x: auto;
      margin-top: 16px;
      padding-bottom: 4px;
      scrollbar-width: none;
    }}
    .market-strip::-webkit-scrollbar {{ display: none; }}
    .market-pill {{
      min-width: 220px;
      padding: 14px 15px;
      border-radius: 20px;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(255,255,255,.06);
      color: #f8f3e8;
    }}
    .market-pill-label {{
      display: block;
      color: rgba(248,243,232,.68);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .10em;
      text-transform: uppercase;
    }}
    .market-pill-value {{
      display: block;
      margin-top: 8px;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 19px;
      line-height: 1.1;
    }}
    .control-card {{
      padding: 20px;
      border-radius: 28px;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(255,255,255,.07);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.06);
      display: flex;
      flex-direction: column;
    }}
    .control-label {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 0 11px;
      border-radius: 999px;
      background: rgba(244,239,226,.12);
      color: #f6e5b8;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .10em;
      text-transform: uppercase;
    }}
    .control-card h2 {{
      margin: 12px 0 0;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 30px;
      line-height: 1;
      letter-spacing: -.04em;
    }}
    .control-copy {{
      margin: 10px 0 0;
      color: rgba(248,243,232,.78);
      font-size: 15px;
      line-height: 1.55;
    }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: end;
      margin-top: 18px;
    }}
    .field {{ display: flex; flex-direction: column; gap: 6px; }}
    label {{
      color: rgba(248,243,232,.72);
      font-size: 13px;
      font-weight: 700;
    }}
    input {{
      width: 126px;
      min-height: 46px;
      padding: 11px 13px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,.16);
      background: rgba(255,255,255,.08);
      color: #f8f3e8;
      font-size: 15px;
      outline: none;
      transition: border-color .18s ease, box-shadow .18s ease, background .18s ease;
    }}
    input:focus {{
      border-color: rgba(225,188,103,.48);
      box-shadow: 0 0 0 3px rgba(225,188,103,.12);
      background: rgba(255,255,255,.12);
    }}
    .btn,
    .ghost-btn {{
      appearance: none;
      border: 0;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 46px;
      padding: 0 18px;
      border-radius: 14px;
      font-weight: 800;
      transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease, background .18s ease;
    }}
    .btn {{
      background: linear-gradient(90deg, #e8c56f, #f0d894);
      color: #11211b;
      box-shadow: 0 14px 28px rgba(191,143,47,.18);
    }}
    .ghost-btn {{
      border: 1px solid rgba(255,255,255,.16);
      color: #f8f3e8;
      background: rgba(255,255,255,.05);
    }}
    .light-btn {{
      background: linear-gradient(90deg, #c59a3a, #e0bb62);
      color: #11211b;
    }}
    .light-ghost-btn {{
      border-color: var(--line);
      color: var(--ink);
      background: rgba(255,255,255,.56);
    }}
    .btn:hover,
    .ghost-btn:hover,
    .btn:active,
    .ghost-btn:active {{
      transform: translateY(-1px);
      box-shadow: 0 16px 30px rgba(19,33,28,.16);
    }}
    .hero-footer {{
      margin-top: 14px;
      color: rgba(248,243,232,.68);
      font-size: 14px;
      line-height: 1.5;
    }}
    .notice {{
      margin-top: 16px;
      padding: 13px 14px;
      border-radius: 16px;
      border: 1px solid rgba(225,188,103,.20);
      background: rgba(225,188,103,.12);
      color: #f8e2b0;
      font-size: 14px;
    }}
    .hero-strip {{
      display: grid;
      gap: 16px;
      margin-top: 16px;
      grid-template-columns: 1.08fr .92fr;
    }}
    .studio-card,
    .editorial-card {{
      position: relative;
      overflow: hidden;
      min-height: 220px;
      padding: 20px;
      border-radius: 28px;
      border: 1px solid rgba(255,255,255,.12);
      background: rgba(255,255,255,.08);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.05);
    }}
    .studio-card::before,
    .editorial-card::before {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        radial-gradient(circle at top right, rgba(225,188,103,.16), transparent 26%),
        radial-gradient(circle at bottom left, rgba(255,255,255,.08), transparent 24%);
    }}
    .editorial-card {{
      display: grid;
      grid-template-columns: 190px 1fr;
      gap: 18px;
      align-items: center;
    }}
    .spotlight-label {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 0 12px;
      border-radius: 999px;
      background: rgba(225,188,103,.18);
      color: #f4d489;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .10em;
      text-transform: uppercase;
    }}
    .studio-card h2,
    .editorial-copy h3 {{
      position: relative;
      z-index: 1;
      margin: 16px 0 8px;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 34px;
      line-height: 1.02;
      letter-spacing: -.04em;
    }}
    .studio-card h2 span {{ color: rgba(248,243,232,.64); font-size: 24px; }}
    .spotlight-meta,
    .kickoff {{
      position: relative;
      z-index: 1;
      color: rgba(248,243,232,.72);
      font-size: 14px;
      line-height: 1.5;
    }}
    .spotlight-badges {{
      position: relative;
      z-index: 1;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }}
    .spotlight-badges span {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 999px;
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.12);
      color: #f8f3e8;
      font-size: 13px;
      font-weight: 700;
    }}
    .studio-card p,
    .editorial-copy p {{
      position: relative;
      z-index: 1;
      margin: 16px 0 0;
      color: rgba(248,243,232,.78);
      font-size: 15px;
      line-height: 1.58;
    }}
    .studio-actions {{
      position: relative;
      z-index: 1;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }}
    .editorial-image {{
      width: 190px;
      height: 190px;
      object-fit: cover;
      border-radius: 26px;
      border: 1px solid rgba(255,255,255,.12);
      background: rgba(255,255,255,.10);
      box-shadow: var(--shadow-soft);
    }}
    .editorial-fallback,
    .article-image-fallback,
    .article-badge-fallback {{
      display: flex;
      align-items: center;
      justify-content: center;
      color: #d7bb76;
      font-weight: 900;
      letter-spacing: .14em;
    }}
    .grid {{
      display: grid;
      gap: 18px;
      margin-top: 22px;
      grid-template-columns: 1.12fr .88fr;
    }}
    .section-grid {{
      display: grid;
      gap: 18px;
      margin-top: 18px;
      grid-template-columns: 1fr 1fr;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 28px;
      background:
        radial-gradient(circle at top right, rgba(191,143,47,.08), transparent 22%),
        linear-gradient(180deg, var(--surface) 0%, var(--surface-strong) 100%);
      padding: 20px;
      box-shadow: var(--shadow);
      color: var(--ink);
    }}
    .section-anchor {{ scroll-margin-top: 24px; }}
    .panel-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .panel-title {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .panel-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 0 12px;
      border-radius: 999px;
      border: 1px solid rgba(191,143,47,.18);
      background: rgba(191,143,47,.10);
      color: var(--gold);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .10em;
    }}
    .panel h2 {{
      margin: 0;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 30px;
      line-height: 1;
      letter-spacing: -.04em;
      color: var(--forest);
    }}
    .section-note,
    .footer {{
      color: var(--muted);
      font-size: 14px;
      line-height: 1.55;
    }}
    .league {{
      color: var(--gold);
      font-size: 14px;
      font-weight: 800;
      letter-spacing: .02em;
    }}
    .table-shell {{
      overflow-x: auto;
      overflow-y: hidden;
      -webkit-overflow-scrolling: touch;
      border-radius: 22px;
      border: 1px solid rgba(19,33,28,.08);
      background: rgba(255,255,255,.60);
    }}
    .prediction-table {{
      width: 100%;
      min-width: 980px;
      border-collapse: collapse;
    }}
    .prediction-table thead th {{
      padding: 16px 18px;
      text-align: left;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .10em;
      color: var(--muted);
      background: rgba(20,56,44,.04);
      border-bottom: 1px solid rgba(19,33,28,.08);
    }}
    .hour-separator td {{
      padding: 12px 18px;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .10em;
      color: var(--forest);
      background: linear-gradient(90deg, rgba(191,143,47,.12), rgba(20,56,44,.05));
      border-bottom: 1px solid rgba(19,33,28,.08);
    }}
    .prediction-row {{
      transition: transform .20s ease, background .20s ease;
    }}
    .prediction-row.pick-home td:first-child,
    .market-pill.pick-home,
    .table-pick.pick-home {{ box-shadow: inset 4px 0 0 rgba(38, 139, 92, .60); }}
    .prediction-row.pick-away td:first-child,
    .market-pill.pick-away,
    .table-pick.pick-away {{ box-shadow: inset 4px 0 0 rgba(97, 142, 174, .60); }}
    .prediction-row.pick-draw td:first-child,
    .market-pill.pick-draw,
    .table-pick.pick-draw {{ box-shadow: inset 4px 0 0 rgba(191, 143, 47, .60); }}
    .prediction-row.pick-mixed td:first-child,
    .market-pill.pick-mixed,
    .table-pick.pick-mixed {{ box-shadow: inset 4px 0 0 rgba(184, 90, 57, .46); }}
    .prediction-row:hover {{
      transform: translateY(-1px);
      background: rgba(255,255,255,.32);
    }}
    .prediction-table td {{
      padding: 18px;
      vertical-align: top;
      border-bottom: 1px solid rgba(19,33,28,.06);
    }}
    .prediction-table tbody tr:last-child td {{ border-bottom: 0; }}
    .row-league {{
      color: var(--gold);
      font-size: 13px;
      font-weight: 800;
    }}
    .row-kickoff,
    .row-meta {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      margin-top: 6px;
    }}
    .row-kickoff-time {{
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 25px;
      line-height: 1;
      letter-spacing: -.03em;
      color: var(--forest);
    }}
    .row-kickoff-date {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .row-match {{
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 22px;
      line-height: 1.12;
      letter-spacing: -.03em;
      color: var(--ink);
    }}
    .row-match span {{
      color: var(--muted);
      font-size: 14px;
      font-weight: 600;
    }}
    .row-why {{
      margin-top: 8px;
      color: #506259;
      font-size: 14px;
      line-height: 1.48;
    }}
    .row-actions {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    .row-link {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 999px;
      border: 1px solid rgba(19,33,28,.10);
      background: rgba(255,255,255,.72);
      color: var(--ink);
      text-decoration: none;
      font-size: 12px;
      font-weight: 800;
    }}
    .row-link:hover {{ background: rgba(255,255,255,.92); }}
    .favorite-toggle {{
      appearance: none;
      border: 1px solid rgba(191,143,47,.22);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 38px;
      min-height: 38px;
      padding: 0 12px;
      border-radius: 999px;
      background: rgba(191,143,47,.08);
      color: var(--gold);
      font-size: 18px;
      font-weight: 900;
      line-height: 1;
      transition: transform .18s ease, border-color .18s ease, background .18s ease, color .18s ease;
    }}
    .favorite-toggle:hover,
    .favorite-toggle.active {{
      transform: translateY(-1px);
      border-color: rgba(191,143,47,.42);
      background: rgba(191,143,47,.16);
      color: #8f6415;
    }}
    .row-form {{
      color: var(--ink);
      font-size: 14px;
      font-weight: 700;
    }}
    .table-pick {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 56px;
      min-height: 56px;
      padding: 0 14px;
      border-radius: 18px;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 24px;
      line-height: 1;
      color: #10211a;
      background: linear-gradient(135deg, #dcb963, #f0d894);
      box-shadow: 0 16px 26px rgba(191,143,47,.16);
      animation: glowPulse 3.6s ease-in-out infinite;
    }}
    .table-pick.pick-home {{ background: linear-gradient(135deg, #5bb889, #bfe0a7); }}
    .table-pick.pick-away {{ background: linear-gradient(135deg, #9bb9d1, #d2e1ea); }}
    .table-pick.pick-draw {{ background: linear-gradient(135deg, #d8b35d, #efd693); }}
    .table-pick.pick-mixed {{ background: linear-gradient(135deg, #e7b18f, #f0d894); }}
    .table-percent {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .confidence-meter {{
      margin-top: 10px;
      width: 88px;
      height: 6px;
      border-radius: 999px;
      background: rgba(19,33,28,.10);
      overflow: hidden;
    }}
    .confidence-meter span {{
      display: block;
      height: 100%;
      border-radius: 999px;
    }}
    .confidence-meter.confidence-elite span {{ background: linear-gradient(90deg, #4aa36f, #c8dd8f); }}
    .confidence-meter.confidence-strong span {{ background: linear-gradient(90deg, #7eabc8, #4aa36f); }}
    .confidence-meter.confidence-medium span {{ background: linear-gradient(90deg, #d6a554, #e7c76d); }}
    .poster {{
      display: block;
      width: 100%;
      border-radius: 22px;
      border: 1px solid rgba(19,33,28,.10);
      background: #f4efe2;
    }}
    .compact-table {{ min-width: 720px; }}
    .compact-table-shell {{ height: 100%; }}
    .match-list {{ display: grid; gap: 14px; }}
    .match-card {{
      padding: 18px;
      border-radius: 24px;
      border: 1px solid rgba(19,33,28,.08);
      background: rgba(255,255,255,.64);
      box-shadow: var(--shadow-soft);
      transition: transform .22s ease, border-color .22s ease, background .22s ease;
    }}
    .match-card:hover {{
      transform: translateY(-4px);
      border-color: rgba(191,143,47,.22);
      background: rgba(255,255,255,.82);
    }}
    .meta-row {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: flex-start;
    }}
    .pick-badge {{
      min-width: 92px;
      padding: 9px 10px;
      border-radius: 18px;
      border: 1px solid rgba(191,143,47,.16);
      background: rgba(191,143,47,.08);
      text-align: center;
    }}
    .pick-value {{
      display: block;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 24px;
      line-height: 1;
      color: var(--forest);
    }}
    .pick-percent {{
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
    }}
    .teams {{
      margin-top: 12px;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 24px;
      line-height: 1.1;
      letter-spacing: -.03em;
    }}
    .teams span {{
      color: var(--muted);
      font-size: 16px;
      font-weight: 600;
    }}
    .stats-grid {{
      margin-top: 14px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .stats-grid div {{
      padding: 11px 12px;
      border-radius: 16px;
      background: rgba(244,239,226,.92);
      border: 1px solid rgba(19,33,28,.08);
    }}
    .stats-grid strong {{
      display: block;
      color: var(--gold);
      font-size: 12px;
      margin-bottom: 5px;
    }}
    .stats-grid span {{
      display: block;
      color: var(--ink);
      font-size: 14px;
      line-height: 1.35;
    }}
    .why {{
      margin-top: 12px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.56;
    }}
    .tennis-state {{
      min-width: 110px;
      padding: 8px 10px;
      border-radius: 14px;
      text-align: center;
      color: #8a651d;
      background: rgba(191,143,47,.10);
      border: 1px solid rgba(191,143,47,.18);
      font-size: 13px;
      font-weight: 700;
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
      border: 1px solid rgba(19,33,28,.08);
      background: rgba(255,255,255,.72);
      box-shadow: var(--shadow-soft);
      transition: transform .22s ease, border-color .22s ease;
    }}
    .article-showcase:hover {{
      transform: translateY(-4px);
      border-color: rgba(191,143,47,.22);
    }}
    .article-visual-wrap {{
      position: relative;
      height: 214px;
      overflow: hidden;
      background: rgba(244,239,226,.92);
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
      background: linear-gradient(180deg, transparent 10%, rgba(255,252,244,.10) 50%, rgba(255,252,244,.78) 100%);
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
      background: rgba(255,252,244,.86);
      border: 1px solid rgba(19,33,28,.10);
      backdrop-filter: blur(12px);
    }}
    .article-copy {{ padding: 18px; }}
    .article-topline {{
      color: var(--gold);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .10em;
      text-transform: uppercase;
    }}
    .article-copy h3 {{
      margin: 10px 0 8px;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 24px;
      line-height: 1.15;
      letter-spacing: -.03em;
      color: var(--forest);
    }}
    .article-copy p {{
      margin: 12px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.58;
    }}
    .article-link {{
      display: inline-flex;
      margin-top: 14px;
      color: var(--forest);
      text-decoration: none;
      font-weight: 800;
    }}
    .favorites-board {{
      border-radius: 24px;
      border: 1px solid rgba(19,33,28,.08);
      background: rgba(255,255,255,.58);
      padding: 18px;
    }}
    .favorites-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}
    .favorite-card {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      min-height: 140px;
      padding: 18px;
      border-radius: 20px;
      border: 1px solid rgba(191,143,47,.16);
      background:
        radial-gradient(circle at top right, rgba(191,143,47,.10), transparent 30%),
        linear-gradient(180deg, rgba(255,255,255,.86), rgba(244,239,226,.92));
      text-decoration: none;
      color: var(--ink);
      transition: transform .20s ease, border-color .20s ease, background .20s ease;
    }}
    .favorite-card:hover {{
      transform: translateY(-3px);
      border-color: rgba(191,143,47,.26);
      background:
        radial-gradient(circle at top right, rgba(191,143,47,.12), transparent 32%),
        linear-gradient(180deg, rgba(255,255,255,.94), rgba(244,239,226,.96));
    }}
    .favorite-card-title {{
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 20px;
      line-height: 1.15;
      letter-spacing: -.03em;
    }}
    .favorite-card-meta {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .favorite-card-pick {{
      margin-top: auto;
      display: inline-flex;
      align-items: center;
      width: max-content;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 999px;
      border: 1px solid rgba(191,143,47,.16);
      background: rgba(191,143,47,.10);
      color: #8c6417;
      font-size: 13px;
      font-weight: 800;
    }}
    .favorites-empty {{
      padding: 20px;
      border-radius: 18px;
      border: 1px dashed rgba(19,33,28,.14);
      background: rgba(244,239,226,.72);
      color: var(--muted);
      font-size: 14px;
      line-height: 1.58;
    }}
    .bottom-dock {{
      position: sticky;
      bottom: 16px;
      z-index: 20;
      display: none;
      justify-content: center;
      margin-top: 18px;
    }}
    .dock-inner {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px;
      border-radius: 999px;
      border: 1px solid rgba(19,33,28,.10);
      background: rgba(255,252,244,.90);
      backdrop-filter: blur(18px);
      box-shadow: 0 18px 40px rgba(19,33,28,.12);
    }}
    .dock-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 84px;
      min-height: 42px;
      padding: 0 14px;
      border-radius: 999px;
      text-decoration: none;
      color: var(--ink);
      font-size: 13px;
      font-weight: 800;
      background: rgba(244,239,226,.86);
      transition: transform .18s ease, background .18s ease;
    }}
    .dock-link:hover,
    .dock-link:active {{
      transform: translateY(-1px);
      background: rgba(191,143,47,.12);
    }}
    .site-footer {{
      margin-top: 24px;
      padding: 22px;
      border-radius: 28px;
      border: 1px solid rgba(19,33,28,.08);
      background:
        radial-gradient(circle at top right, rgba(191,143,47,.10), transparent 24%),
        linear-gradient(180deg, rgba(255,252,244,.90), rgba(248,241,226,.94));
      display: grid;
      grid-template-columns: 1.1fr .9fr;
      gap: 20px;
      box-shadow: var(--shadow);
    }}
    .footer-brand h3 {{
      margin: 0;
      font-family: "Bricolage Grotesque", sans-serif;
      font-size: 34px;
      line-height: 1;
      letter-spacing: -.04em;
      color: var(--forest);
    }}
    .footer-brand p,
    .footer-links a {{
      color: var(--muted);
      font-size: 14px;
      line-height: 1.58;
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
      from {{ opacity: 0; transform: translateY(24px) scale(.985); }}
      to {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}
    @keyframes floatIn {{
      from {{ opacity: 0; transform: translateY(36px) scale(.96); }}
      to {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}
    @keyframes glowPulse {{
      0%, 100% {{ box-shadow: 0 16px 26px rgba(191,143,47,.16); }}
      50% {{ box-shadow: 0 18px 34px rgba(191,143,47,.22); }}
    }}
    @media (max-width: 1080px) {{
      h1 {{ font-size: 50px; }}
      .hero-overview,
      .hero-strip,
      .grid,
      .section-grid,
      .site-footer {{
        grid-template-columns: 1fr;
      }}
      .hero-stats {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .articles-grid,
      .favorites-grid {{
        grid-template-columns: 1fr;
      }}
      .editorial-card {{
        grid-template-columns: 1fr;
      }}
      .editorial-image {{
        width: 100%;
        height: 220px;
      }}
    }}
    @media (max-width: 720px) {{
      .wrap {{ padding: 16px 14px 104px; }}
      .hero {{ padding: 22px 18px; }}
      .hero-top {{ grid-template-columns: 1fr; }}
      .hero-media {{ min-height: 210px; }}
      .hero-media-image {{ min-height: 210px; }}
      h1 {{ font-size: 38px; }}
      .sub {{ font-size: 16px; }}
      .hero-stats {{ grid-template-columns: 1fr 1fr; }}
      .hero-quickstrip {{ display: grid; grid-template-columns: 1fr; }}
      .toolbar {{ align-items: stretch; }}
      .field,
      input,
      .btn,
      .ghost-btn {{
        width: 100%;
      }}
      .table-shell,
      .compact-table-shell {{
        overflow-x: auto;
      }}
      .prediction-table {{ min-width: 760px; }}
      .bottom-dock {{ display: flex; }}
      .nav-links {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero float-in">
      <div class="hero-top">
        <div>
          <span class="hero-kicker">Control room GABFOOT</span>
          <h1>Des picks mieux cadres, plus credibles et plus lisibles qu&apos;un portail surcharge.</h1>
          <div class="sub">Le dashboard GABFOOT rassemble les matchs les plus solides, la Botola, le tennis, les news et le poster Telegram dans une seule salle de controle. Mise a jour: {now}</div>
          <div class="nav-links">
            <a class="nav-link" href="/">Accueil</a>
            <a class="nav-link" href="#top-picks">Top picks</a>
            <a class="nav-link" href="#botola-zone">Botola</a>
            <a class="nav-link" href="#tennis-zone">Tennis</a>
            <a class="nav-link" href="#articles-zone">Articles</a>
          </div>
          <div class="hero-quickstrip">
            <span>Board live</span>
            <span>Botola + Tennis</span>
            <span>Poster Telegram</span>
          </div>
        </div>
        {dashboard_hero_visual}
      </div>

      <div class="hero-overview">
        <div>
          <div class="hero-stats">
            <div class="hero-stat reveal">
              <div class="hero-stat-label">Top picks</div>
              <div class="hero-stat-value">{len(matches)}</div>
              <div class="hero-stat-note">Matchs gardes dans le tri principal.</div>
            </div>
            <div class="hero-stat reveal">
              <div class="hero-stat-label">Botola</div>
              <div class="hero-stat-value">{len(botola)}</div>
              <div class="hero-stat-note">Signaux marocains actifs aujourd&apos;hui.</div>
            </div>
            <div class="hero-stat reveal">
              <div class="hero-stat-label">Tennis</div>
              <div class="hero-stat-value">{len(tennis)}</div>
              <div class="hero-stat-note">Rencontres ATP et WTA surveillees.</div>
            </div>
            <div class="hero-stat reveal">
              <div class="hero-stat-label">Articles</div>
              <div class="hero-stat-value">{len(articles)}</div>
              <div class="hero-stat-note">Angles editoriaux pour donner du relief.</div>
            </div>
          </div>
          <div class="market-strip">
            {market_strip}
          </div>
          {notice_html}
        </div>

        <aside class="control-card">
          <span class="control-label">Pilotage rapide</span>
          <h2>Regle le board en quelques secondes.</h2>
          <p class="control-copy">Le panneau reste volontairement simple: tu filtres le volume, tu ajustes le seuil de confiance et tu peux pousser le poster Telegram sans sortir du dashboard.</p>
          <form class="toolbar" method="get" action="/dashboard">
            <div class="field">
              <label for="limit">Nombre de matchs</label>
              <input id="limit" name="limit" type="number" min="1" max="12" value="{limit}">
            </div>
            <div class="field">
              <label for="min_percent">Seuil minimal</label>
              <input id="min_percent" name="min_percent" type="number" min="55" max="90" value="{min_percent}">
            </div>
            <button class="btn" type="submit">Mettre a jour</button>
            <a class="ghost-btn" href="/send?limit={limit}&min_percent={min_percent}">Envoyer sur Telegram</a>
          </form>
          <div class="hero-footer">Tu restes sur une logique premium: peu de bruit, un tri plus net, et une sortie Telegram qui garde la meme coherence visuelle.</div>
        </aside>
      </div>

      <div class="hero-strip">
        {lead_panel}
        {editorial_panel}
      </div>
    </section>

    <div class="grid">
      <section id="top-picks" class="panel reveal section-anchor">
        <div class="panel-head">
          <div class="panel-title">
            <span class="panel-pill">Premium board</span>
            <h2>Matchs les plus solides</h2>
          </div>
          <span class="footer">{len(matches)} match(s) retenu(s) dans ce run</span>
        </div>
        <p class="section-note">La lecture reste immediate: horaire, contexte, recommandation, taux de confiance et lien direct vers la fiche match.</p>
        {prediction_table}
      </section>
      {image_block}
    </div>

    <div class="section-grid">
      <section id="botola-zone" class="panel reveal section-anchor">
        <div class="panel-head">
          <div class="panel-title">
            <span class="panel-pill">Maroc</span>
            <h2>Signal Botola Pro</h2>
          </div>
          <span class="footer">Premiere ligue marocaine</span>
        </div>
        <p class="section-note">Une entree locale plus forte pour donner au produit une personnalite claire et un axe marocain visible.</p>
        {botola_table}
      </section>
      <section id="tennis-zone" class="panel reveal section-anchor">
        <div class="panel-head">
          <div class="panel-title">
            <span class="panel-pill">Multi-board</span>
            <h2>Tennis world</h2>
          </div>
          <span class="footer">ATP / WTA</span>
        </div>
        <p class="section-note">Le board secondaire montre que GABFOOT peut ouvrir d&apos;autres flux sans perdre sa direction visuelle.</p>
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
            <h2>Radar clubs & joueurs</h2>
          </div>
          <span class="footer">Actualites football</span>
        </div>
        <p class="section-note">Le produit ressemble moins a une simple grille de cotes quand l&apos;editorial est traite avec la meme exigence visuelle que les picks.</p>
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
        <p>Le dashboard prend maintenant une direction plus haut de gamme: sable chaud, vert profond, accent or et hierarchie beaucoup plus nette. L&apos;objectif n&apos;est pas de faire plus charge qu&apos;un gros portail de pronostics, mais de rendre le signal plus clair et plus credible.</p>
      </div>
      <div class="footer-links">
        <a href="/">Retour a l'accueil</a>
        <a href="#top-picks">Ouvrir les top picks</a>
        <a href="#favorites-zone">Voir mes favoris</a>
        <a href="#botola-zone">Board Botola</a>
        <a href="#articles-zone">Radar editorial</a>
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
    :root {{
      --canvas: #f4f0e7;
      --canvas-2: #efe5d4;
      --paper: rgba(255, 252, 246, 0.84);
      --paper-strong: #fffdf8;
      --ink: #10281f;
      --ink-soft: #5f6f66;
      --forest: #1e5b43;
      --gold: #c89a2b;
      --line: rgba(16, 40, 31, 0.10);
      --line-strong: rgba(16, 40, 31, 0.18);
      --shadow: 0 24px 60px rgba(19, 39, 30, 0.12);
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0; font-family:"Source Sans 3", "DejaVu Sans", sans-serif; color:var(--ink);
      background:
        radial-gradient(circle at 0% 0%, rgba(200,154,43,.18), transparent 28%),
        radial-gradient(circle at 100% 10%, rgba(30,91,67,.12), transparent 22%),
        linear-gradient(180deg, var(--canvas) 0%, var(--canvas-2) 100%);
    }}
    a {{ color:inherit; }}
    .wrap {{ max-width:1240px; margin:0 auto; padding:16px 20px 56px; }}
    .hero, .panel {{
      border:1px solid rgba(255,255,255,.46); border-radius:30px; background:var(--paper);
      box-shadow:var(--shadow); backdrop-filter: blur(14px);
    }}
    .hero {{ padding:28px; position:relative; overflow:hidden; }}
    .hero::before {{
      content:""; position:absolute; inset:0;
      background:
        radial-gradient(circle at 16% 0%, rgba(200,154,43,.14), transparent 28%),
        radial-gradient(circle at 100% 10%, rgba(30,91,67,.12), transparent 22%),
        linear-gradient(90deg, transparent 14%, rgba(30,91,67,.08) 14.4%, rgba(30,91,67,.08) 14.8%, transparent 15.2%, transparent 84.8%, rgba(30,91,67,.08) 85.2%, rgba(30,91,67,.08) 85.6%, transparent 86%);
      pointer-events:none;
    }}
    .hero-layout {{
      position:relative; z-index:1; display:grid; grid-template-columns:minmax(0, 1.15fr) minmax(280px, .85fr); gap:22px; align-items:start;
    }}
    h1 {{
      margin:10px 0 0; font-family:"Bricolage Grotesque", sans-serif; font-size:clamp(2.5rem, 5vw, 4rem);
      line-height:.95; letter-spacing:-.04em; color:var(--ink);
    }}
    .eyebrow {{
      display:inline-flex; width:max-content; min-height:34px; align-items:center; padding:0 12px; border-radius:999px;
      background:rgba(200,154,43,.14); color:#885f1b; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.08em;
    }}
    .sub {{ margin-top:12px; color:var(--ink-soft); position:relative; z-index:1; font-size:16px; line-height:1.6; max-width:720px; }}
    .links {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:20px; }}
    .links a {{
      color:var(--ink); text-decoration:none; border:1px solid var(--line); border-radius:999px; padding:10px 14px;
      background:rgba(255,255,255,.68); transition: transform .18s ease, background .18s ease, border-color .18s ease;
      position:relative; z-index:1;
    }}
    .links a:hover, .links a:active {{ transform:translateY(-1px); background:var(--paper-strong); border-color:var(--line-strong); }}
    .hero-summary {{
      padding:20px; border-radius:24px; border:1px solid var(--line);
      background:linear-gradient(180deg, rgba(255,255,255,.78), rgba(255,255,255,.58));
      display:grid; gap:14px;
    }}
    .hero-summary strong {{
      display:block; font-family:"Bricolage Grotesque", sans-serif; font-size:28px; line-height:1.02; letter-spacing:-.03em;
    }}
    .hero-summary p {{ margin:0; color:var(--ink-soft); font-size:14px; line-height:1.6; }}
    .hero-summary ul {{ margin:0; padding-left:18px; color:var(--ink-soft); display:grid; gap:8px; }}
    .panel {{ padding:22px; margin-top:18px; }}
    .match-list {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:16px; align-items:start; }}
    .match-card {{
      padding:18px; border-radius:24px; background:linear-gradient(180deg, rgba(255,255,255,.88), rgba(255,250,241,.92));
      border:1px solid var(--line); box-shadow: inset 0 1px 0 rgba(255,255,255,.62);
      transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
      display:flex; flex-direction:column; min-height:100%;
    }}
    .match-card:hover {{ transform:translateY(-2px); border-color:var(--line-strong); box-shadow:0 18px 34px rgba(20,57,43,.10); }}
    .meta-row {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; }}
    .league {{ color:#8b6220; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.08em; }}
    .kickoff {{ color:var(--ink-soft); font-size:13px; margin-top:4px; }}
    .pick-badge {{ min-width:92px; border-radius:18px; background:rgba(255,255,255,.78); padding:8px 10px; border:1px solid rgba(16,40,31,.10); text-align:center; }}
    .pick-value {{ display:block; font-size:24px; font-weight:800; color:var(--forest); }}
    .pick-percent {{ display:block; font-size:13px; color:var(--ink-soft); }}
    .teams, .article-title {{
      margin-top:12px; font-family:"Bricolage Grotesque", sans-serif; font-size:24px; font-weight:700; line-height:1.08; letter-spacing:-.03em;
    }}
    .teams span {{ color:var(--ink-soft); font-size:16px; font-weight:500; }}
    .stats-grid {{ margin-top:14px; display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:10px; }}
    .stats-grid div {{ padding:11px 12px; border-radius:16px; background:rgba(255,255,255,.58); border:1px solid rgba(16,40,31,.08); }}
    .stats-grid strong {{ display:block; color:#8b6220; font-size:12px; margin-bottom:5px; text-transform:uppercase; letter-spacing:.08em; }}
    .stats-grid span {{ display:block; color:var(--ink); font-size:14px; line-height:1.35; }}
    .why {{ margin-top:12px; font-size:14px; color:var(--ink-soft); flex:1 1 auto; }}
    .tennis-state {{ min-width:110px; padding:8px 10px; border-radius:14px; text-align:center; color:var(--forest); background:rgba(30,91,67,.08); border:1px solid rgba(30,91,67,.20); font-size:13px; font-weight:700; }}
    .article-link {{ display:inline-flex; margin-top:14px; color:var(--forest); text-decoration:none; font-weight:800; }}
    @media (max-width: 980px) {{
      .hero-layout, .match-list {{ grid-template-columns:1fr; }}
      .stats-grid {{ grid-template-columns:1fr; }}
      .teams, .article-title {{ font-size:20px; }}
    }}
    @media (max-width: 760px) {{
      .wrap {{ padding:14px 14px 34px; }}
      .hero {{ padding:22px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="hero-layout">
        <div>
          <span class="eyebrow">Section GABFOOT</span>
          <h1>{html.escape(title)}</h1>
          <div class="sub">{html.escape(subtitle)}</div>
          <div class="links">
            <a href="/dashboard">Dashboard</a>
            <a href="/botola">Botola Pro</a>
            <a href="/tennis">Tennis World</a>
            <a href="/articles">Articles clubs & joueurs</a>
          </div>
        </div>
        <aside class="hero-summary">
          <strong>Structure plus nette</strong>
          <p>Cette section reprend maintenant la meme logique visuelle que la home: un en-tete clair, une grille stable et des cartes alignees.</p>
          <ul>
            <li>hierarchie de lecture immediate</li>
            <li>cartes de contenu alignees verticalement</li>
            <li>palette et architecture coherentes avec le reste du site</li>
          </ul>
        </aside>
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


def render_update_cards(updates: list[dict[str, object]]) -> str:
    cards = []
    for item in updates:
        bullets = item.get("bullets", [])
        bullet_html = "".join(f"<li>{html.escape(str(bullet))}</li>" for bullet in bullets[:4])
        cards.append(
            f"""
            <article class="match-card">
              <div class="meta-row">
                <div>
                  <div class="league">{html.escape(str(item.get("label", "Produit")))}</div>
                  <div class="kickoff">{html.escape(str(item.get("date", "")))}</div>
                </div>
                <div class="pick-badge">
                  <span class="pick-value">NEW</span>
                  <span class="pick-percent">update</span>
                </div>
              </div>
              <div class="article-title">{html.escape(str(item.get("title", "")))}</div>
              <div class="why">{html.escape(str(item.get("summary", "")))}</div>
              <ul class="why" style="margin:12px 0 0 18px;">{bullet_html}</ul>
            </article>
            """
        )
    return "".join(cards) or '<div class="why">Aucune mise a jour publiee pour le moment.</div>'


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        limit = max(1, min(12, int(params.get("limit", ["6"])[0])))
        min_percent = max(55, min(90, int(params.get("min_percent", ["78"])[0])))
        site_url = request_base_url(self)

        if parsed.path == "/healthz":
            with _CACHE_LOCK:
                cache_entries = len(_DASHBOARD_CACHE)
            return self.render_json({"ok": True, "cacheEntries": cache_entries, "siteUrl": site_url})

        if parsed.path == "/robots.txt":
            return self.render_robots(site_url)

        if parsed.path == "/sitemap.xml":
            return self.render_sitemap(site_url)

        if parsed.path == "/image":
            return self.serve_image()

        if parsed.path == "/dashboard-hero.jpg":
            return self.serve_file(DASHBOARD_HERO_IMAGE_PATH, "image/jpeg", cache_control="public, max-age=86400")

        if parsed.path == "/icon.png":
            return self.serve_file(ICON_PATH, "image/png", cache_control="public, max-age=86400")

        if parsed.path == "/manifest.webmanifest":
            return self.render_manifest()

        if parsed.path == "/sw.js":
            return self.render_sw()

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
            return self.render_dashboard(matches, card_path, limit, min_percent, notice, botola, tennis, articles, site_url=site_url)

        if parsed.path == "/api/dashboard":
            force_refresh = "_refresh" in params or params.get("force", ["0"])[0] == "1"
            if force_refresh:
                cached_payload = get_fresh_dashboard_payload(limit=limit, min_percent=min_percent)
                stale = False
            else:
                cached_payload, stale = get_cached_dashboard_payload(limit=limit, min_percent=min_percent)
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
            cached_payload, stale = get_cached_dashboard_payload(limit=6, min_percent=min_percent)
            articles = list((cached_payload or {}).get("articles", []))
            subtitle = "Actualites football, clubs et joueurs"
            if stale:
                subtitle += " - donnees en cache"
            return self.render_html(render_collection("Articles clubs & joueurs", subtitle, render_article_cards(articles)))

        if parsed.path == "/updates":
            subtitle = "Journal produit GABFOOT, nouveautes visibles et mises a jour de la plateforme"
            return self.render_html(render_collection("Nouveautes & mises a jour", subtitle, render_update_cards(load_site_updates())))

        force_refresh = "_refresh" in params or params.get("force", ["0"])[0] == "1"
        if force_refresh:
            cached_payload = get_fresh_dashboard_payload(limit=limit, min_percent=min_percent)
            stale = False
        else:
            cached_payload, stale = get_cached_dashboard_payload(limit=limit, min_percent=min_percent)

        if cached_payload is None:
            matches = []
            card_path = CARD_PATH if CARD_PATH.exists() else None
            notice = "Chargement initial en cours. Recharge la page dans quelques secondes."
            botola = []
            tennis = []
            articles = []
        else:
            matches = [deserialize_match(item) for item in cached_payload.get("matches", [])]
            card_path = CARD_PATH if cached_payload.get("has_card") and CARD_PATH.exists() else None
            notice = "Donnees en cache, actualisation en cours." if stale else ""
            botola = list(cached_payload.get("botola", []))
            tennis = list(cached_payload.get("tennis", []))
            articles = list(cached_payload.get("articles", []))

        if parsed.path == "/":
            return self.render_html(landing_html(matches, card_path, limit, min_percent, notice, botola, tennis, articles, site_url=site_url))

        if parsed.path in {"/dashboard", "/app"}:
            return self.render_dashboard(matches, card_path, limit, min_percent, notice, botola, tennis, articles, site_url=site_url)

        self.send_error(HTTPStatus.NOT_FOUND, "Page introuvable")
        return

    def serve_image(self) -> None:
        return self.serve_file(CARD_PATH, "image/png")

    def render_bytes(
        self,
        payload: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        cache_control: str = "no-store, max-age=0",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(payload)

    def serve_file(self, path: Path, content_type: str, cache_control: str = "no-store, max-age=0") -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Fichier indisponible")
            return
        payload = path.read_bytes()
        self.render_bytes(payload, content_type, cache_control=cache_control)

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
        site_url: str = "",
    ) -> None:
        payload = page_html(matches, card_path, limit, min_percent, notice, botola, tennis, articles, site_url=site_url).encode("utf-8")
        self.render_bytes(payload, "text/html; charset=utf-8")

    def render_html(self, html_text: str) -> None:
        payload = html_text.encode("utf-8")
        self.render_bytes(payload, "text/html; charset=utf-8")

    def render_json(self, data: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.render_bytes(payload, "application/json; charset=utf-8", status=status)

    def render_robots(self, site_url: str) -> None:
        payload = f"User-agent: *\nAllow: /\nSitemap: {absolute_url(site_url, '/sitemap.xml')}\n".encode("utf-8")
        self.render_bytes(payload, "text/plain; charset=utf-8", cache_control="public, max-age=3600")

    def render_sitemap(self, site_url: str) -> None:
        static_entries = [
            ("/", "daily", "1.0"),
            ("/dashboard", "hourly", "0.9"),
            ("/botola", "daily", "0.8"),
            ("/tennis", "hourly", "0.7"),
            ("/articles", "daily", "0.8"),
        ]
        dynamic_entries: list[tuple[str, str, str]] = []
        seen_paths: set[str] = set()
        with _CACHE_LOCK:
            cached_entries = list(_DASHBOARD_CACHE.values())
        for entry in cached_entries:
            for item in entry.get("matches", []):
                match_id = int(item.get("match_id", 0))
                league_id = int(item.get("league_id", 0))
                home_id = int(item.get("home_id", 0))
                away_id = int(item.get("away_id", 0))
                for path in (
                    f"/match/{match_id}" if match_id else "",
                    f"/league/{league_id}" if league_id else "",
                    f"/team/{home_id}" if home_id else "",
                    f"/team/{away_id}" if away_id else "",
                ):
                    if path and path not in seen_paths:
                        seen_paths.add(path)
                        dynamic_entries.append((path, "daily", "0.6"))
                if len(dynamic_entries) >= 24:
                    break
            if len(dynamic_entries) >= 24:
                break

        lastmod = datetime.now(timezone.utc).date().isoformat()
        urls = []
        for path, changefreq, priority in [*static_entries, *dynamic_entries]:
            urls.append(
                f"<url><loc>{html.escape(absolute_url(site_url, path))}</loc><lastmod>{lastmod}</lastmod><changefreq>{changefreq}</changefreq><priority>{priority}</priority></url>"
            )
        payload = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(urls)
            + "</urlset>"
        ).encode("utf-8")
        self.render_bytes(payload, "application/xml; charset=utf-8", cache_control="public, max-age=3600")

    def render_manifest(self) -> None:
        public_url = configured_public_url()
        payload = json.dumps(
            {
                "name": "GABFOOT Dashboard",
                "short_name": "GABFOOT",
                "id": absolute_url(public_url, "/dashboard") if public_url else "/dashboard",
                "start_url": "/dashboard",
                "scope": "/",
                "display": "standalone",
                "background_color": "#08150c",
                "theme_color": "#08150c",
                "description": "Dashboard GABFOOT pour les matchs surs, l'affiche Telegram et le controle des boards.",
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
        self.render_bytes(payload, "application/manifest+json; charset=utf-8", cache_control="public, max-age=3600")

    def render_sw(self) -> None:
        payload = b"""self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""
        self.render_bytes(payload, "application/javascript; charset=utf-8", cache_control="public, max-age=3600")

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
