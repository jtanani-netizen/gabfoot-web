#!/usr/bin/env python3
import os, requests
from datetime import datetime
from types import SimpleNamespace
from PIL import Image, ImageDraw, ImageFont

TOKEN="8560959679:AAE8h-0zaVcYESCKyX-t9Q6i7eeoHp9_MYk"
CHAT_ID="6817196309"
BG="/home/jibril/match_analyzer/cards/model.jpg"  # ton fond terrain (810x1440 portrait)
OUT="/home/jibril/match_analyzer/cards/card_portrait.png"

accent=(255,214,70)
text=(255,255,255)
bg_shadow=(0,0,0,190)


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

    fH=font('DejaVuSans-Bold.ttf',72)
    fT=font('DejaVuSans-Bold.ttf',52)
    fB=font('DejaVuSansMono.ttf',38)

    header="GABFOOT 🦁⚽"; hw,_=d.textbbox((0,0),header,font=fH)[2:]
    d.text(((W-hw)/2,50),header,fill=accent,font=fH)
    ttxt="⚽ "+title; tw,_=d.textbbox((0,0),ttxt,font=fT)[2:]
    d.text(((W-tw)/2,140),ttxt,fill=text,font=fT)
    d.rectangle([40,200,W-40,204],fill=accent)

    y=230; block_h=190; margin=40
    for idx,ev in enumerate(batch[:6],1):
        x0,x1=margin,W-margin
        d.rounded_rectangle([x0,y,x1,y+block_h],radius=18,outline=accent,width=3)
        d.text((x0+16,y+10),f"{idx}) {ev.home} vs {ev.away}",fill=text,font=fT)
        comp=f"{ev.tournament} | {ev.date.strftime('%H:%M')}"; cw=d.textbbox((0,0),comp,font=fB)[2]
        d.text((x1-16-cw,y+18),comp,fill=accent,font=fB)
        score=f"{ev.ph}-{ev.pa}"; sw=d.textbbox((0,0),score,font=fT)[2]
        d.text((x0+(x1-x0-sw)/2,y+70),score,fill=accent,font=fT)
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
    path=generate_card(batch,"Matchs en vue — portrait simple")
    with open(path,'rb') as f:
        r=requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            files={'photo':f},
            data={'chat_id':CHAT_ID,'caption':'⚽ GABFOOT — portrait simple, texte lisible'} )
    print(r.status_code, r.text)

if __name__=='__main__':
    main()
