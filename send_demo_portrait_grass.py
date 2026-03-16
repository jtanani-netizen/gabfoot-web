#!/usr/bin/env python3
import os, requests
from datetime import datetime
from types import SimpleNamespace
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from project_paths import CARDS_DIR, ENV_FILE

load_dotenv(ENV_FILE)

TOKEN=os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID=os.getenv("TELEGRAM_CHAT_ID", "")
BG=str(CARDS_DIR / "bg_grass.png")  # 1080x1920 portrait placeholder
OUT=str(CARDS_DIR / "card_portrait_grass.png")

accent=(255,214,70)
text=(255,255,255)
bg_shadow=(0,0,0,170)


def font(name, size):
    try: return ImageFont.truetype(name, size)
    except: return ImageFont.load_default()


def generate_card(batch, title="Matchs en vue"):
    base=Image.open(BG).convert("RGB")
    W,H=base.size
    img=base.convert("RGBA")
    overlay=Image.new("RGBA",(W,H),bg_shadow)
    img=Image.alpha_composite(img,overlay).convert("RGB")
    d=ImageDraw.Draw(img)

    fH=font('DejaVuSans-Bold.ttf',80)
    fT=font('DejaVuSans-Bold.ttf',56)
    fB=font('DejaVuSansMono.ttf',40)

    header="GABFOOT 🦁⚽"; hw,_=d.textbbox((0,0),header,font=fH)[2:]
    d.text(((W-hw)/2,60),header,fill=accent,font=fH)
    ttxt="⚽ "+title; tw,_=d.textbbox((0,0),ttxt,font=fT)[2:]
    d.text(((W-tw)/2,150),ttxt,fill=text,font=fT)
    d.rectangle([40,220,W-40,224],fill=accent)

    y=250; block_h=190; margin=40
    for idx,ev in enumerate(batch[:6],1):
        x0,x1=margin,W-margin
        d.rounded_rectangle([x0,y,x1,y+block_h],radius=18,outline=accent,width=3)
        d.text((x0+16,y+10),f"{idx}) {ev.home} vs {ev.away}",fill=text,font=fT)
        comp=f"{ev.tournament} | {ev.date.strftime('%H:%M')}"; cw=d.textbbox((0,0),comp,font=fB)[2]
        d.text((x1-16-cw,y+18),comp,fill=accent,font=fB)
        score=f"{ev.ph}-{ev.pa}"; sw=d.textbbox((0,0),score,font=fT)[2]
        d.text((x0+(x1-x0-sw)/2,y+80),score,fill=accent,font=fT)
        d.text((x0+24,y+130),f"{ev.home:<12} G% {ev.hg}",fill=text,font=fB)
        d.text((x0+360,y+130),f"{ev.away:<12} G% {ev.ag}",fill=text,font=fB)
        y+=block_h+16

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT)
    return OUT


def main():
    demo=[
        ("PSG","Marseille","Ligue 1",60,40,2,1),
        ("Barça","Real Madrid","LaLiga",65,55,2,1),
        ("Man City","Liverpool","EPL",70,60,2,1),
        ("Bayern","Dortmund","Bundes",62,48,2,1),
        ("Inter","Milan","Serie A",58,52,1,1),
        ("Ajax","PSV","Eredivisie",57,43,2,0),
    ]
    batch=[SimpleNamespace(home=h,away=a,tournament=comp,date=datetime.utcnow(),
                           hg=hg,ag=ag,ph=ph,pa=pa) for h,a,comp,hg,ag,ph,pa in demo]
    path=generate_card(batch,"Matchs en vue — fond gazon sombre")
    if TOKEN and CHAT_ID:
        with open(path,'rb') as f:
            r=requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                files={'photo':f},
                data={'chat_id':CHAT_ID,'caption':'⚽ GABFOOT — fond gazon sombre (portrait)'} )
        print(r.status_code, r.text)
    else:
        print(path)

if __name__=='__main__':
    main()
