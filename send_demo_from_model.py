#!/usr/bin/env python3
import os, requests
from datetime import datetime
from types import SimpleNamespace
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from project_paths import CARDS_DIR, ENV_FILE

load_dotenv(ENV_FILE)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BG_PATH = str(CARDS_DIR / "model.jpg")  # portrait 810x1440
OUT_PATH = str(CARDS_DIR / "card_batch.png")

accent = (247, 189, 45)   # doré doux
text   = (240, 244, 248)


def font(name, size):
    try: return ImageFont.truetype(name, size)
    except: return ImageFont.load_default()


def generate_card(batch, title="Matchs en vue"):
    base = Image.open(BG_PATH).convert("RGB")
    # garder portrait pour respecter le modèle
    W, H = base.size
    img = base.copy()
    draw = ImageDraw.Draw(img)

    fH = font("DejaVuSans-Bold.ttf", 80)
    fT = font("DejaVuSans-Bold.ttf", 54)
    fB = font("DejaVuSansMono.ttf", 40)

    # bandeau haut (sur 200 px environ)
    header = "GABFOOT 🦁⚽"
    hw,_ = draw.textbbox((0,0), header, font=fH)[2:]
    draw.text(((W-hw)/2, 60), header, fill=accent, font=fH)

    title_text = "⚽ " + title
    tw,_ = draw.textbbox((0,0), title_text, font=fT)[2:]
    draw.text(((W-tw)/2, 180), title_text, fill=text, font=fT)

    # zone matches : 6 blocs empilés
    margin = 60
    block_h = 180
    y = 260
    for idx, ev in enumerate(batch[:6], 1):
        x0, x1 = margin, W - margin
        draw.rounded_rectangle([x0, y, x1, y + block_h], radius=18, outline=accent, width=3)

        # titre + compo
        draw.text((x0 + 20, y + 12), f"{idx}) {ev.home} vs {ev.away}", fill=text, font=fT)
        comp = f"{ev.tournament} | {ev.date.strftime('%H:%M UTC')}"
        cw = draw.textbbox((0,0), comp, font=fB)[2:][0]
        draw.text((x1 - 20 - cw, y + 20), comp, fill=accent, font=fB)

        # score XXL
        score = f"{ev.ph} - {ev.pa}"
        sw = draw.textbbox((0,0), score, font=fT)[2:][0]
        draw.text((x0 + (x1 - x0 - sw) / 2, y + 80), score, fill=accent, font=fT)

        # stats simples
        draw.text((x0 + 40, y + 130), f"{ev.home:<12} G% {ev.hg}", fill=text, font=fB)
        draw.text((x0 + 340, y + 130), f"{ev.away:<12} G% {ev.ag}", fill=text, font=fB)

        y += block_h + 18

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    img.save(OUT_PATH)
    return OUT_PATH


def main():
    demo = [
        ("PSG", "Marseille", "Ligue 1", "20:00", 60, 40, 2, 1),
        ("Barça", "Real Madrid", "LaLiga", "21:00", 65, 55, 2, 1),
        ("Man City", "Liverpool", "EPL", "19:45", 70, 60, 2, 1),
        ("Bayern", "Dortmund", "Bundes", "18:30", 62, 48, 2, 1),
        ("Inter", "Milan", "Serie A", "20:45", 58, 52, 1, 1),
        ("Ajax", "PSV", "Eredivisie", "19:00", 57, 43, 2, 0),
    ]
    batch = [SimpleNamespace(home=h, away=a, tournament=comp, date=datetime.utcnow(),
                             ph=ph, pa=pa, hg=hg, ag=ag)
             for h, a, comp, t, hg, ag, ph, pa in demo]

    path = generate_card(batch, "Matchs en vue — modèle importé")
    if TOKEN and CHAT_ID:
        with open(path, 'rb') as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                files={'photo': f},
                data={'chat_id': CHAT_ID, 'caption': '⚽ GABFOOT — modèle importé (portrait)'}
            )
        print(r.status_code, r.text)
    else:
        print(path)


if __name__ == "__main__":
    main()
