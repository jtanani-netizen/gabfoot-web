#!/usr/bin/env python3
import os
from datetime import datetime
from types import SimpleNamespace

import requests
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from project_paths import CARDS_DIR, ENV_FILE

load_dotenv(ENV_FILE)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OUT = str(CARDS_DIR / "card_model_style.png")
BG_PATH = str(CARDS_DIR / "bg_football_photo.jpg")

BG = (10, 10, 14)
ACCENT = (170, 255, 110)
ACCENT_SOFT = (220, 255, 190)
TEXT = (245, 245, 245)
MUTED = (198, 198, 205)


def font(name: str, size: int):
    try:
        return ImageFont.truetype(name, size)
    except Exception:
        return ImageFont.load_default()


def load_background(w: int, h: int) -> Image.Image:
    if os.path.exists(BG_PATH):
        return Image.open(BG_PATH).convert("RGB").resize((w, h))
    return Image.new("RGB", (w, h), BG)


def draw_ball(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(245, 245, 245), outline=(20, 20, 20), width=3)
    pentagon = [
        (cx, cy - r // 3),
        (cx + r // 3, cy - r // 10),
        (cx + r // 5, cy + r // 4),
        (cx - r // 5, cy + r // 4),
        (cx - r // 3, cy - r // 10),
    ]
    draw.polygon(pentagon, fill=(20, 20, 20))


def draw_waving_mascot(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    body = (70, 210, 120)
    hat = (120, 255, 150)
    skin = (245, 220, 190)
    dark = (20, 40, 20)
    white = (250, 250, 250)

    draw.rounded_rectangle([x + 18, y + 40, x + 58, y + 92], radius=16, fill=body, outline=dark, width=3)
    draw.ellipse([x + 18, y + 4, x + 58, y + 44], fill=skin, outline=dark, width=3)
    draw.polygon([(x + 14, y + 16), (x + 38, y - 12), (x + 62, y + 16)], fill=hat, outline=dark)
    draw.ellipse([x + 29, y + 18, x + 35, y + 24], fill=dark)
    draw.ellipse([x + 41, y + 18, x + 47, y + 24], fill=dark)
    draw.arc([x + 29, y + 24, x + 47, y + 34], start=10, end=170, fill=dark, width=2)
    draw.line([x + 18, y + 52, x + 4, y + 38], fill=body, width=6)
    draw.line([x + 4, y + 38, x + 0, y + 20], fill=body, width=6)
    draw.ellipse([x - 8, y + 10, x + 10, y + 28], fill=skin, outline=dark, width=2)
    draw.line([x + 58, y + 56, x + 72, y + 66], fill=body, width=6)
    draw.line([x + 28, y + 92, x + 20, y + 112], fill=body, width=6)
    draw.line([x + 46, y + 92, x + 54, y + 112], fill=body, width=6)
    draw.ellipse([x + 14, y + 108, x + 26, y + 116], fill=white, outline=dark, width=1)
    draw.ellipse([x + 48, y + 108, x + 60, y + 116], fill=white, outline=dark, width=1)


def generate_card(batch, title="Affiche pronostics", out_path: str = OUT):
    w, h = 1080, 1600
    base = load_background(w, h)
    overlay = Image.new("RGBA", (w, h), (8, 24, 8, 95))
    img = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    f_header = font("DejaVuSans-Bold.ttf", 78)
    f_title = font("DejaVuSans-Bold.ttf", 36)
    f_big = font("DejaVuSans-Bold.ttf", 70)
    f_body = font("DejaVuSans.ttf", 30)
    f_small = font("DejaVuSans.ttf", 24)
    f_meta = font("DejaVuSans.ttf", 22)

    draw.rectangle([0, 0, w, 210], fill=(12, 28, 12))
    draw.rectangle([70, 185, w - 70, 190], fill=ACCENT)

    header = "GABFOOT"
    sub = title
    hw = draw.textbbox((0, 0), header, font=f_header)[2]
    sw = draw.textbbox((0, 0), sub, font=f_title)[2]
    header_x = (w - hw) / 2
    header_y = 52
    # Slightly softer green and a dark backing strip improve readability at the top.
    draw.rounded_rectangle([header_x - 18, header_y - 8, header_x + hw + 86, header_y + 92], radius=18, fill=(8, 16, 8))
    draw.text((header_x, header_y), header, fill=ACCENT, font=f_header)
    draw_ball(draw, int(header_x + hw + 48), int(header_y + 42), 24)
    draw_waving_mascot(draw, 42, 46)
    draw.text(((w - sw) / 2, 132), sub, fill=(245, 245, 245), font=f_title)

    top_y = 225
    margin_x = 44
    gap_x = 24
    gap_y = 22
    block_w = (w - margin_x * 2 - gap_x) // 2
    block_h = 378
    for idx, ev in enumerate(batch[:6], 1):
        pos = idx - 1
        col = pos % 2
        row = pos // 2
        x0 = margin_x + col * (block_w + gap_x)
        y = top_y + row * (block_h + gap_y)
        x1 = x0 + block_w
        y1 = y + block_h

        draw.rounded_rectangle([x0, y, x1, y1], radius=22, fill=(14, 18, 14), outline=ACCENT, width=3)
        draw.text((x0 + 18, y + 16), f"{idx}) {ev.home} vs {ev.away}", fill=TEXT, font=f_small)

        tournament_text = ev.tournament
        time_text = ev.date.strftime('%H:%M')
        t_w = draw.textbbox((0, 0), tournament_text, font=f_meta)[2]
        time_w = draw.textbbox((0, 0), time_text, font=f_meta)[2]
        draw.text((x0 + 18, y + 48), tournament_text, fill=(235, 235, 235), font=f_meta)
        draw.text((x1 - time_w - 18, y + 48), time_text, fill=(255, 255, 255), font=f_meta)

        score_label_y = y + 70
        score_value_y = y + 110
        draw.text((x0 + 18, score_label_y), "Score", fill=MUTED, font=f_small)
        draw.text((x0 + 18, score_value_y), f"{ev.ph}-{ev.pa}", fill=ACCENT, font=f_big)

        draw.rounded_rectangle([x0 + 18, y + 174, x1 - 18, y + 208], radius=10, fill=(20, 34, 20))
        draw.text((x0 + 26, y + 181), "Forme", fill=ACCENT, font=f_meta)
        draw.text((x0 + 98, y + 181), f"{ev.home} {ev.hg}%  |  {ev.away} {ev.ag}%", fill=TEXT, font=f_meta)

        # Compact details row 1
        pill_y1 = y + 228
        pill_h = 36
        pill_gap = 10
        pill1 = (x0 + 18, pill_y1, x0 + 210, pill_y1 + pill_h)
        pill2 = (x0 + 228, pill_y1, x1 - 18, pill_y1 + pill_h)
        for rect in (pill1, pill2):
            draw.rounded_rectangle(rect, radius=10, fill=(22, 28, 22))
        draw.text((pill1[0] + 10, pill_y1 + 8), f"PMT {ev.ht_pick}", fill=ACCENT_SOFT, font=f_meta)
        draw.text((pill2[0] + 10, pill_y1 + 8), f"Exact {ev.exact}", fill=TEXT, font=f_meta)

        # Compact details row 2
        pill_y2 = y + 274
        pill4 = (x0 + 18, pill_y2, x0 + 210, pill_y2 + pill_h)
        pill5 = (x0 + 228, pill_y2, x1 - 18, pill_y2 + pill_h)
        for rect in (pill4, pill5):
            draw.rounded_rectangle(rect, radius=10, fill=(22, 28, 22))
        draw.text((pill4[0] + 10, pill_y2 + 8), f"DC {ev.dc}  |  +2.5 {ev.over25}", fill=ACCENT, font=f_meta)
        draw.text((pill5[0] + 10, pill_y2 + 8), f"Gagnant {ev.winner}", fill=ACCENT_SOFT, font=f_meta)

        # Bottom score cases: easier to read than one compact line.
        band_y0 = y + 312
        title_y = band_y0
        draw.text((x0 + 18, title_y), "3 scores", fill=MUTED, font=f_meta)
        score_box_y0 = y + 336
        score_box_y1 = y + 366
        inner_gap = 10
        inner_w = x1 - x0 - 36
        score_box_w = (inner_w - 2 * inner_gap) // 3
        score_boxes = [
            (x0 + 18, score_box_y0, x0 + 18 + score_box_w, score_box_y1),
            (x0 + 18 + score_box_w + inner_gap, score_box_y0, x0 + 18 + 2 * score_box_w + inner_gap, score_box_y1),
            (x1 - 18 - score_box_w, score_box_y0, x1 - 18, score_box_y1),
        ]
        score_values = [ev.exact, ev.score2, ev.score3]
        for rect, value in zip(score_boxes, score_values):
            draw.rounded_rectangle(rect, radius=10, fill=(18, 22, 18))
            value_w = draw.textbbox((0, 0), value, font=f_meta)[2]
            value_x = rect[0] + ((rect[2] - rect[0] - value_w) / 2)
            draw.text((value_x, score_box_y0 + 4), value, fill=TEXT, font=f_meta)

    footer = "Pronostics • style simple"
    fw = draw.textbbox((0, 0), footer, font=f_small)[2]
    draw.text(((w - fw) / 2, h - 56), footer, fill=MUTED, font=f_small)

    img.save(out_path, quality=95)
    return out_path


def main():
    demo = [
        ("PSG", "Marseille", "Ligue 1", 60, 40, 2, 1, "PSG", "1", "2-1", "1-0", "2-0", "PSG", "Oui"),
        ("Barca", "Real Madrid", "LaLiga", 65, 55, 2, 1, "Nul", "X", "2-1", "1-1", "2-0", "Barca", "Oui"),
        ("Man City", "Liverpool", "Premier League", 70, 60, 2, 1, "Man City", "1", "2-1", "1-0", "2-0", "Man City", "Oui"),
        ("Bayern", "Dortmund", "Bundesliga", 62, 48, 2, 1, "Nul", "X", "2-1", "1-1", "2-0", "Bayern", "Oui"),
        ("Inter", "Milan", "Serie A", 58, 52, 1, 1, "Inter", "1X", "1-1", "1-0", "0-0", "Nul", "Non"),
        ("Ajax", "PSV", "Eredivisie", 57, 43, 2, 0, "Ajax", "1", "2-0", "1-0", "2-1", "Ajax", "Non"),
    ]
    batch = [
        SimpleNamespace(
            home=h,
            away=a,
            tournament=comp,
            date=datetime.utcnow(),
            hg=hg,
            ag=ag,
            ph=ph,
            pa=pa,
            ht=ht,
            ht_pick=ht_pick,
            exact=exact,
            score2=score2,
            score3=score3,
            winner=winner,
            dc=ht_pick if ht_pick in {"1X", "X2", "12"} else ("1X" if winner not in {"Monaco"} else "X2"),
            over25=over25,
        )
        for h, a, comp, hg, ag, ph, pa, ht, ht_pick, exact, score2, score3, winner, over25 in demo
    ]
    path = generate_card(batch)
    if TOKEN and CHAT_ID:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                files={"photo": f},
                data={"chat_id": CHAT_ID, "caption": "Retour au style du debut"},
                timeout=60,
            )
        print(r.status_code, r.text)
    else:
        print(path)


if __name__ == "__main__":
    main()
