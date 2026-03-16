#!/usr/bin/env python3
import os, requests
from types import SimpleNamespace
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

TOKEN = "8560959679:AAE8h-0zaVcYESCKyX-t9Q6i7eeoHp9_MYk"
CHAT_ID = "6817196309"
BG_PATH = "/home/jibril/match_analyzer/cards/bg_maradona.jpg"
OUT_PATH = "/home/jibril/match_analyzer/cards/card_batch.png"

accent = (0, 186, 255)
text = (235, 240, 245)


def font(name, size):
    try:
        return ImageFont.truetype(name, size)
    except Exception:
        return ImageFont.load_default()


def draw_bar(draw, x, y, w, h, pct, color):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=(35, 45, 65))
    fill_w = max(6, min(w, int(w * pct / 100)))
    draw.rounded_rectangle([x, y, x + fill_w, y + h], radius=10, fill=color)


def load_bg(W, H):
    im = Image.open(BG_PATH).convert("RGB").resize((W, H))
    return im


def generate_card(batch, title="Matchs en vue"):
    W, H = 2560, 1440
    margin = 200
    gutter = 60
    cols = 2
    block_w = (W - 2 * margin - gutter) // cols
    block_h = 300

    def trunc(s, n=14):
        return s if len(s) <= n else s[:n - 1] + "…"

    img = load_bg(W, H).convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 170))
    img = Image.alpha_composite(img, overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    fH = font("DejaVuSans-Bold.ttf", 90)
    fT = font("DejaVuSans-Bold.ttf", 72)
    fB = font("DejaVuSansMono.ttf", 40)

    header = "GABFOOT 🦁⚽"
    hw, hh = draw.textbbox((0, 0), header, font=fH)[2:]
    draw.text(((W - hw) / 2, 70), header, fill=accent, font=fH)

    title_text = "⚽ " + title
    tw, _ = draw.textbbox((0, 0), title_text, font=fT)[2:]
    draw.text(((W - tw) / 2, 230), title_text, fill=text, font=fT)
    draw.rectangle([margin, 300, W - margin, 306], fill=accent)

    y_start = 280
    for idx, ev in enumerate(batch[:6]):
        r = idx // cols
        c = idx % cols
        x0 = margin + c * (block_w + gutter)
        y0 = y_start + r * (block_h + 30)
        x1 = x0 + block_w
        y1 = y0 + block_h

        draw.rounded_rectangle([x0, y0, x1, y1], radius=18, outline=accent, width=3)

        h_form = ev.home_form
        a_form = ev.away_form

        draw.text((x0 + 24, y0 + 8), f"{idx + 1}) {trunc(ev.home)} vs {trunc(ev.away)}", fill=text, font=fT)
        comp_line = f"{trunc(ev.tournament, 12)} | {ev.date.strftime('%H:%M UTC')}"
        cw = draw.textbbox((0, 0), comp_line, font=fB)[2]
        draw.text((x1 - 24 - cw, y0 + 20), comp_line, fill=accent, font=fB)

        ph = 2 if h_form['G%'] > a_form['G%'] else 1
        pa = 1 if h_form['G%'] > a_form['G%'] else 2
        if abs(h_form['G%'] - a_form['G%']) < 5:
            ph = pa = 1
        sw = draw.textbbox((0, 0), f"{ph} - {pa}", font=fT)[2]
        draw.text((x0 + (block_w - sw) / 2, y0 + 90), f"{ph} - {pa}", fill=accent, font=fT)

        def color_for(g):
            if g >= 55: return (46, 204, 113)
            if g >= 45: return (243, 156, 18)
            return (231, 76, 60)

        hcol = color_for(h_form['G%']); acol = color_for(a_form['G%'])

        draw.text((x0 + 200, y0 + 70), f"{trunc(ev.home, 12):<12} G% {h_form['G%']:.0f}", fill=hcol, font=fB)
        draw_bar(draw, x0 + 200, y0 + 110, block_w - 400, 22, h_form['G%'], hcol)
        draw.text((x0 + 200, y0 + 154), f"{trunc(ev.away, 12):<12} G% {a_form['G%']:.0f}", fill=acol, font=fB)
        draw_bar(draw, x0 + 200, y0 + 194, block_w - 400, 22, a_form['G%'], acol)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    img.save(OUT_PATH)
    return OUT_PATH


def main():
    demo = [
        ('PSG', 'Marseille', 'Ligue 1', '20:00', 60, 40),
        ('Barça', 'Real Madrid', 'LaLiga', '21:00', 55, 65),
        ('Man City', 'Liverpool', 'EPL', '19:45', 70, 60),
        ('Bayern', 'Dortmund', 'Bundes', '18:30', 62, 48),
        ('Inter', 'Milan', 'Serie A', '20:45', 58, 52),
        ('Ajax', 'PSV', 'Eredivisie', '19:00', 57, 43),
    ]
    batch = []
    for i, (h, a, comp, t, hg, ag) in enumerate(demo, 1):
        batch.append(SimpleNamespace(
            home=h, away=a, tournament=comp, date=datetime.utcnow(),
            home_id=i*2-1, away_id=i*2, home_logo='', away_logo='', league_logo='',
            home_form={'G%': hg, 'N%': 25, 'P%': 15},
            away_form={'G%': ag, 'N%': 25, 'P%': 15}
        ))

    path = generate_card(batch, "Matchs en vue — fond stade")
    with open(path, 'rb') as f:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            files={'photo': f},
            data={'chat_id': CHAT_ID, 'caption': '⚽ GABFOOT — 2560x1440 (texte agrandi)'}
        )
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
