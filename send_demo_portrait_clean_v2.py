#!/usr/bin/env python3
import os, requests
from datetime import datetime
from types import SimpleNamespace
from PIL import Image, ImageDraw, ImageFont

TOKEN="8560959679:AAE8h-0zaVcYESCKyX-t9Q6i7eeoHp9_MYk"
CHAT_ID="6817196309"
OUT="/home/jibril/match_analyzer/cards/card_portrait_clean_v2.png"

accent=(255,214,70)
text=(255,255,255)
bg=(8,8,12)


def font(name, size):
    try: return ImageFont.truetype(name, size)
    except: return ImageFont.load_default()


def generate_card(batch, title="Matchs en vue"):
    W,H=810,1440
    img=Image.new("RGB", (W,H), bg)
    d=ImageDraw.Draw(img)

    fH=font('DejaVuSans-Bold.ttf',78)
    fT=font('DejaVuSans-Bold.ttf',60)
    fS=font('DejaVuSans-Bold.ttf',80)
    fB=font('DejaVuSansMono.ttf',42)

    header="GABFOOT 🦁⚽"; hw,_=d.textbbox((0,0),header,font=fH)[2:]
    d.text(((W-hw)/2,40), header, fill=accent, font=fH)
    ttxt="⚽ "+title; tw,_=d.textbbox((0,0),ttxt,font=fT)[2:]
    d.text(((W-tw)/2,130), ttxt, fill=text, font=fT)
    d.rectangle([20, 200, W-20, 204], fill=accent)

    y=230; block_h=180; margin=20
    for idx, ev in enumerate(batch[:6], 1):
        x0,x1=margin, W-margin
        d.rounded_rectangle([x0,y,x1,y+block_h], radius=16, outline=accent, width=3)
        d.text((x0+12,y+6), f"{idx}) {ev.home} vs {ev.away}", fill=text, font=fT)
        comp=f"{ev.tournament} | {ev.date.strftime('%H:%M')}"; cw=d.textbbox((0,0),comp,font=fB)[2]
        d.text((x1-12-cw,y+16), comp, fill=accent, font=fB)

        score=f"{ev.ph}-{ev.pa}"; sw=d.textbbox((0,0),score,font=fS)[2]
        d.text((x0+(x1-x0-sw)/2, y+60), score, fill=accent, font=fS)

        stats=f"G% {ev.hg} | {ev.ag}"
        stw=d.textbbox((0,0),stats,font=fB)[2]
        d.text((x0+(x1-x0-stw)/2, y+130), stats, fill=text, font=fB)

        y+=block_h+14

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
    path=generate_card(batch,"Matchs en vue — fond uni clair")
    with open(path,'rb') as f:
        r=requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            files={'photo':f},
            data={'chat_id':CHAT_ID,'caption':'⚽ GABFOOT — fond uni, scores XXL (portrait)'} )
    print(r.status_code, r.text)

if __name__=='__main__':
    main()
