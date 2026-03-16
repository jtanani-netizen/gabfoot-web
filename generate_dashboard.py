#!/usr/bin/env python3
import os
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
OUT = 'cards/dashboard.png'
BG_URL = 'https://images.unsplash.com/photo-1508609349937-5ec4ae374ebf?auto=format&fit=crop&w=1920&q=80'

# Palette
TEXT = (255, 255, 255)
ACCENT = (0, 186, 255)
PANEL = (20, 40, 60, 180)
OUTLINE = (200, 200, 200)

# Sample data
matches = [
    {
        'competition': 'Süper Lig - Turkey',
        'home': 'Galatasaray', 'away': 'Fenerbahçe',
        'status': "Live 45' +3", 'score': '0 - 0', 'pred': 'Predicted 1 - 0',
        'prob': (52, 28, 20), 'pick': '1'
    },
    {
        'competition': 'Premier League',
        'home': 'Arsenal', 'away': 'Liverpool',
        'status': 'Kick-Off 19:00', 'score': '–', 'pred': 'Predicted 2 - 2',
        'prob': (40, 30, 30), 'pick': 'X'
    },
    {
        'competition': 'La Liga',
        'home': 'Real Madrid', 'away': 'Barcelona',
        'status': 'FT Yesterday', 'score': '3 - 1', 'pred': 'Predicted 2 - 1',
        'prob': (48, 27, 25), 'pick': '1'
    },
    {
        'competition': 'Serie A',
        'home': 'Inter', 'away': 'Juventus',
        'status': 'Kick-Off 21:45', 'score': '–', 'pred': 'Predicted 1 - 1',
        'prob': (35, 33, 32), 'pick': '1X'
    },
    {
        'competition': 'Bundesliga',
        'home': 'Bayern', 'away': 'Dortmund',
        'status': 'Live 12\'', 'score': '0 - 1', 'pred': 'Predicted 2 - 2',
        'prob': (45, 25, 30), 'pick': 'X'
    },
    {
        'competition': 'Ligue 1',
        'home': 'PSG', 'away': 'Marseille',
        'status': 'Kick-Off 22:00', 'score': '–', 'pred': 'Predicted 3 - 1',
        'prob': (62, 21, 17), 'pick': '1'
    },
]

# Fonts
try:
    FONT_HEADER = ImageFont.truetype('DejaVuSans-Bold.ttf', 64)
    FONT_TITLE = ImageFont.truetype('DejaVuSans-Bold.ttf', 36)
    FONT_TEXT = ImageFont.truetype('DejaVuSans.ttf', 28)
    FONT_SMALL = ImageFont.truetype('DejaVuSans.ttf', 22)
except:
    FONT_HEADER = FONT_TITLE = FONT_TEXT = FONT_SMALL = ImageFont.load_default()

# Load background
try:
    r = requests.get(BG_URL, timeout=10)
    r.raise_for_status()
    bg = Image.open(BytesIO(r.content)).convert('RGB').resize((W, H))
except Exception:
    # fallback gradient green
    bg = Image.new('RGB', (W, H), (10, 40, 10))
    dr = ImageDraw.Draw(bg)
    for y in range(H):
        g = int(40 + (60 * y / H))
        dr.line([(0, y), (W, y)], fill=(10, g, 10))

# Dark overlay for readability
overlay = Image.new('RGBA', (W, H), (0, 0, 0, 140))
img = Image.alpha_composite(bg.convert('RGBA'), overlay)

# Draw
draw = ImageDraw.Draw(img)

# Header
header = 'Football Predictions - 14 March 2026'
hw = draw.textlength(header, font=FONT_HEADER)
draw.text(((W - hw) / 2, 30), header, font=FONT_HEADER, fill=(255, 255, 255))

# Layout
start_y = 130
row_h = 150
padding_x = 80
box_radius = 16

# helper for team badge placeholder
import random
def badge(color):
    b = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(b)
    d.ellipse([0, 0, 64, 64], fill=color)
    return b

def bar(x, y, w, h, pct, color):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h//2, fill=(60, 60, 60))
    draw.rounded_rectangle([x, y, x + int(w * pct / 100), y + h], radius=h//2, fill=color)

for i, m in enumerate(matches):
    y = start_y + i * row_h
    box = [padding_x, y, W - padding_x, y + row_h - 20]
    draw.rounded_rectangle(box, radius=box_radius, outline=OUTLINE, width=2, fill=PANEL)

    # badges
    home_badge = badge((0, 170, 255))
    away_badge = badge((255, 80, 80))
    img.paste(home_badge, (padding_x + 20, y + 15), home_badge)
    img.paste(away_badge, (padding_x + 20, y + 80), away_badge)

    # team names
    draw.text((padding_x + 100, y + 20), m['home'], font=FONT_TITLE, fill=TEXT)
    draw.text((padding_x + 100, y + 90), m['away'], font=FONT_TITLE, fill=TEXT)

    # competition + status
    draw.text((padding_x + 450, y + 10), m['competition'], font=FONT_SMALL, fill=(200, 200, 200))
    draw.text((padding_x + 450, y + 40), m['status'], font=FONT_SMALL, fill=ACCENT)

    # score current / predicted
    draw.text((padding_x + 450, y + 80), f"Score: {m['score']}", font=FONT_TEXT, fill=TEXT)
    draw.text((padding_x + 450, y + 120), m['pred'], font=FONT_TEXT, fill=ACCENT)

    # bars win/draw/lose
    bx = padding_x + 900; by = y + 30; bw = 320; bh = 18
    win, drawp, lose = m['prob']
    bar(bx, by, bw, bh, win, (60, 200, 100))
    bar(bx, by + 28, bw, bh, drawp, (240, 200, 70))
    bar(bx, by + 56, bw, bh, lose, (230, 80, 80))
    draw.text((bx + bw + 20, by - 4), f"{win}%", font=FONT_SMALL, fill=TEXT)
    draw.text((bx + bw + 20, by + 24), f"{drawp}%", font=FONT_SMALL, fill=TEXT)
    draw.text((bx + bw + 20, by + 52), f"{lose}%", font=FONT_SMALL, fill=TEXT)

    # pick
    draw.text((bx, by + 90), f"Pick: {m['pick']}", font=FONT_TEXT, fill=ACCENT)

img.save(OUT)
print('Saved', OUT)

# Send to Telegram if credentials available
token = os.getenv('TELEGRAM_BOT_TOKEN')
chat_id = os.getenv('TELEGRAM_CHAT_ID')
if token and chat_id:
    with open(OUT, 'rb') as f:
        resp = requests.post(
            f'https://api.telegram.org/bot{token}/sendPhoto',
            data={'chat_id': chat_id, 'caption': 'Tableau de bord pronostics'},
            files={'photo': f}
        )
    try:
        resp.raise_for_status()
        print('Envoyé sur Telegram')
    except Exception as e:
        print('Envoi Telegram échoué:', e, resp.text if 'resp' in locals() else '')
