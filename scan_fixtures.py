#!/usr/bin/env python3
"""Scanne les prochaines affiches de ligues majeures et envoie un résumé Telegram.

Config via .env ou variables d'environnement :
- API_FOOTBALL_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID obligatoires
- LEAGUES (csv d'ID de ligues API-Football, ex: "39,61,140")
- NEXT_MATCHES (nombre total d'affiches à envoyer, défaut 3)
- LAST_N (nombre de matchs utilisés pour la forme, défaut 5)
"""

import os
import logging
import urllib.parse
from typing import List

import sys
import site
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

# ensure user-installed packages are visible when run under systemd
sys.path.append(site.getusersitepackages())
sys.path.extend([
    "/home/jibril/snap/codex/30/.local/lib/python3.12/site-packages",
    os.path.expanduser("~/.local/lib/python3.12/site-packages"),
])

import requests
from dotenv import load_dotenv

from api_football import fixtures_next, last_events, ApiFootballError
from analyze import send_telegram, summarize
from project_paths import CARDS_DIR


def parse_leagues() -> List[int]:
    raw = os.getenv("LEAGUES", "61,39,140")  # Ligue1, Premier League, La Liga
    out = []
    for part in raw.split(','):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out or [61, 39, 140]


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

    next_matches = int(os.getenv("NEXT_MATCHES", 3))
    last_n = int(os.getenv("LAST_N", 5))

    leagues = parse_leagues()
    all_events = []
    for lg in leagues:
        try:
            evs = fixtures_next(lg, count=next_matches)
            all_events.extend(evs)
        except ApiFootballError as exc:
            logging.error("League %s: %s", lg, exc)
        except requests.HTTPError as exc:
            logging.error("League %s HTTP: %s", lg, exc)

    if not all_events:
        logging.warning("Aucun match à venir trouvé (check quota/clé/API)")
        return

    # dédoublonnage par id et tri global par date
    uniq = {ev.id: ev for ev in all_events}
    events = sorted(uniq.values(), key=lambda e: e.date)

    def load_font(name: str, size: int):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            try:
                return ImageFont.truetype("DejaVuSans.ttf", size)
            except Exception:
                return ImageFont.load_default()

    def color_for_winrate(g_pct: float):
        if g_pct >= 55:
            return (46, 204, 113)  # green
        if g_pct >= 45:
            return (243, 156, 18)  # orange
        return (231, 76, 60)       # red

    def fetch_logo(url: str, size=(72, 72)):
        if not url:
            return None
        try:
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content)).convert("RGBA")
            img.thumbnail(size, Image.LANCZOS)
            return img
        except Exception:
            return None

    def draw_bar(draw, x, y, w, h, pct, color):
        draw.rounded_rectangle([x, y, x+w, y+h], radius=6, fill=(45, 60, 80))
        fill_w = max(4, min(w, int(w * pct / 100)))
        draw.rounded_rectangle([x, y, x+fill_w, y+h], radius=6, fill=color)

    def load_bg(W, H):
        bg_path = str(CARDS_DIR / "bg_maradona.jpg")
        if not os.path.exists(bg_path):
            url = "https://i.imgur.com/5pZ01qV.jpg"
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                os.makedirs(os.path.dirname(bg_path), exist_ok=True)
                with open(bg_path, "wb") as f:
                    f.write(r.content)
            except Exception:
                return None
        try:
            im = Image.open(bg_path).convert("RGB")
            im = im.resize((W, H))
            return im
        except Exception:
            return None

    def generate_card(batch, title="Matchs en vue"):
        W, H = 1920, 1200
        accent = (0, 186, 255)
        text = (235, 240, 245)
        margin = 140

        bg_image = load_bg(W, H)
        if bg_image is None:
            bg_image = Image.new("RGB", (W, H), (10, 15, 25))
        img = bg_image.convert("RGBA")
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 180))
        img = Image.alpha_composite(img, overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        font_header = load_font("DejaVuSans-Bold.ttf", 72)
        font_title = load_font("DejaVuSans-Bold.ttf", 44)
        font_body = load_font("DejaVuSansMono.ttf", 30)

        header_text = "GABFOOT 🦁⚽"
        hw, hh = draw.textsize(header_text, font=font_header)
        draw.text(((W - hw) / 2, 50), header_text, fill=accent, font=font_header)

        draw.text((margin, 150), "⚽ " + title, fill=text, font=font_title)
        draw.rectangle([margin, 200, W - margin, 204], fill=accent)

        box_h = 180
        y = 230
        for idx, ev in enumerate(batch, 1):
            try:
                home_form = summarize(last_events(ev.home_id, last_n), ev.home_id)
                away_form = summarize(last_events(ev.away_id, last_n), ev.away_id)
            except Exception as exc:
                logging.error("Forme échouée pour %s vs %s: %s", ev.home, ev.away, exc)
                continue

            draw.rounded_rectangle([margin, y, W - margin, y + box_h - 20], radius=18, outline=accent, width=3)

            draw.text((margin + 20, y + 8), f"{idx}) {ev.home} vs {ev.away}", fill=text, font=font_title)
            draw.text((W - margin - 400, y + 8), f"{ev.tournament[:14]} | {ev.date.strftime('%H:%M UTC')}", fill=accent, font=font_title)

            home_logo = fetch_logo(ev.home_logo)
            away_logo = fetch_logo(ev.away_logo)
            if home_logo:
                img.paste(home_logo, (margin + 30, y + 40), home_logo)
            if away_logo:
                img.paste(away_logo, (W - margin - 120, y + 40), away_logo)

            hcol = color_for_winrate(home_form['G%'])
            acol = color_for_winrate(away_form['G%'])

            pred_home = 2 if home_form['G%'] > away_form['G%'] else 1
            pred_away = 1 if home_form['G%'] > away_form['G%'] else 2
            if abs(home_form['G%'] - away_form['G%']) < 5:
                pred_home = pred_away = 1

            draw.text((W / 2 - 40, y + 54), f"{pred_home} - {pred_away}", fill=accent, font=font_title)

            draw.text((margin + 200, y + 40), f"{ev.home[:14]:<14} G% {home_form['G%']:.0f} N% {home_form['N%']:.0f} P% {home_form['P%']:.0f}", fill=hcol, font=font_body)
            draw_bar(draw, margin + 200, y + 72, 520, 18, home_form['G%'], hcol)

            draw.text((margin + 200, y + 110), f"{ev.away[:14]:<14} G% {away_form['G%']:.0f} N% {away_form['N%']:.0f} P% {away_form['P%']:.0f}", fill=acol, font=font_body)
            draw_bar(draw, margin + 200, y + 142, 520, 18, away_form['G%'], acol)

            y += box_h

        out_dir = CARDS_DIR
        os.makedirs(out_dir, exist_ok=True)
        path = out_dir / "card_batch.png"
        img.save(path)
        return str(path)

    def send_batch(batch, title="Matchs en vue"):
        photo_path = generate_card(batch, title)
        if not os.getenv("TELEGRAM_BOT_TOKEN"):
            logging.warning("Pas de token Telegram; envoi ignoré")
            return
        with open(photo_path, 'rb') as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendPhoto",
                files={'photo': f},
                data={'chat_id': os.getenv('TELEGRAM_CHAT_ID'), 'caption': title}
            )
        if resp.status_code >= 300:
            logging.error("sendPhoto failed %s %s", resp.status_code, resp.text)
            body = title + "\n" + "\n".join(ev.home for ev in batch)
            send_telegram(body)

    # envoyer par lots de 3
    batch = []
    for ev in events:
        if len(batch) < 3:
            batch.append(ev)
        if len(batch) == 3:
            send_batch(batch)
            batch = []
    if batch:
        send_batch(batch, title="Matchs en vue (reste)")


if __name__ == "__main__":
    main()
