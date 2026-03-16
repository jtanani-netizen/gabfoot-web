#!/usr/bin/env python3
import os, requests
from types import SimpleNamespace
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from project_paths import CARDS_DIR, ENV_FILE

load_dotenv(ENV_FILE)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BG_PATH = str(CARDS_DIR / "bg_maradona.jpg")
OUT_PATH = str(CARDS_DIR / "card_batch.png")

accent = (0, 200, 255)
text = (240, 244, 248)


def font(name, size):
    try: return ImageFont.truetype(name, size)
    except: return ImageFont.load_default()


def load_bg(W, H):
    im = Image.open(BG_PATH).convert("RGB").resize((W, H))
    return im


def generate_card(batch, title="Matchs en vue"):
    W, H = 2560, 1440
    margin = 200
    block_h = 220

    img = load_bg(W, H).convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 190))
    img = Image.alpha_composite(img, overlay).convert("RGB")
    d = ImageDraw.Draw(img)

    fH = font("DejaVuSans-Bold.ttf", 120)
    fT = font("DejaVuSans-Bold.ttf", 70)
    fB = font("DejaVuSansMono.ttf", 48)

    header = "GABFOOT 🦁⚽"
    hw, _ = d.textbbox((0, 0), header, font=fH)[2:]
    d.text(((W - hw) / 2, 80), header, fill=accent, font=fH)

    ttxt = "⚽ " + title
    tw, _ = d.textbbox((0, 0), ttxt, font=fT)[2:]
    d.text(((W - tw) / 2, 230), ttxt, fill=text, font=fT)
    d.rectangle([margin, 300, W - margin, 304], fill=accent)

    y = 340
    for idx, ev in enumerate(batch[:6], 1):
        x0, x1 = margin, W - margin
        d.rounded_rectangle([x0, y, x1, y + block_h], radius=18, outline=accent, width=3)

        # titre ligne 1
        d.text((x0 + 30, y + 16), f"{idx}) {ev.home} vs {ev.away}", fill=text, font=fT)
        comp = f"{ev.tournament} | {ev.date.strftime('%H:%M UTC')}"
        cw = d.textbbox((0, 0), comp, font=fB)[2]
        d.text((x1 - 30 - cw, y + 28), comp, fill=accent, font=fB)

        # score XL
        score = f"{ev.ph} - {ev.pa}"
        sw = d.textbbox((0, 0), score, font=fT)[2]
        d.text((x0 + (x1 - x0 - sw) / 2, y + 80), score, fill=accent, font=fT)

        # stats simples (pas de barres)
        d.text((x0 + 60, y + 150), f"{ev.home:14} G% {ev.hg}", fill=text, font=fB)
        d.text((x0 + 60, y + 190), f"{ev.away:14} G% {ev.ag}", fill=text, font=fB)

        y += block_h + 30

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
                             ph=ph, pa=pa, hg=hg, ag=ag,
                             home_form={'G%': hg}, away_form={'G%': ag})
             for h, a, comp, t, hg, ag, ph, pa in demo]

    path = generate_card(batch, "Matchs en vue — minimal grand")
    if TOKEN and CHAT_ID:
        with open(path, 'rb') as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                files={'photo': f},
                data={'chat_id': CHAT_ID, 'caption': '⚽ GABFOOT — minimal 2560x1440 (score XXL)'}
            )
        print(r.status_code, r.text)
    else:
        print(path)


if __name__ == "__main__":
    main()
