# Analyseur de match football

Analyse locale en Python d'un match entre deux equipes a partir de sources gratuites.

Sources utilisees:
- FotMob: forme recente, classement, effectif, top joueurs, actualites.
- TheSportsDB: historique des confrontations directes.

## Installation

```bash
cd /home/jibril/gabfoot-web-upload-20260316
python3 -m venv .venv
./.venv/bin/python -m pip install --target /home/jibril/gabfoot-web-upload-20260316/.venv/lib/python3.12/site-packages -r requirements.txt
cp .env.example .env
```

## Utilisation

```bash
./.venv/bin/python analyze.py "PSG" "Marseille"
./.venv/bin/python analyze.py "Arsenal" "Chelsea" --telegram
```

## Interface web locale

```bash
cd /home/jibril/gabfoot-web-upload-20260316
./.venv/bin/python web_app.py
```

Puis ouvre:

```text
http://127.0.0.1:8012
```

Vitrine publique:

```text
http://127.0.0.1:8012/
```

Dashboard operationnel:

```text
http://127.0.0.1:8012/dashboard
```

## Deploiement gratuit sur Render

Le projet est pret pour un deploiement Render avec le fichier `render.yaml`.

Etapes:

```bash
cd /home/jibril/gabfoot-web-upload-20260316
python3 -m py_compile web_app.py
```

Puis:

1. pousse ce dossier sur GitHub;
2. connecte le repo a Render;
3. cree un nouveau Blueprint ou Web Service depuis ce repo;
4. Render utilisera `render.yaml`;
5. le site sera publie sur un sous-domaine `*.onrender.com`;
6. renseigne `GABFOOT_PUBLIC_URL` dans Render avec l'URL publique finale.

Le code detecte automatiquement la variable `PORT` de Render et ecoute sur `0.0.0.0` en production.

Le blueprint declare aussi:
- `healthCheckPath: /healthz`
- `GABFOOT_PUBLIC_URL` pour les URLs canoniques
- `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` si tu veux garder l'envoi Telegram

Endpoints utiles apres mise en ligne:

```text
/healthz
/robots.txt
/sitemap.xml
```

## Domaine gratuit EU.org

Pour brancher un domaine gratuit du type `gabfoot.eu.org`:

1. cree un compte sur `https://nic.eu.org/arf/`;
2. demande un domaine via la procedure EU.org;
3. attends la validation manuelle par email;
4. ajoute le domaine dans Render, section `Custom Domains`;
5. configure le DNS EU.org vers le sous-domaine `*.onrender.com` fourni par Render;
6. une fois le domaine valide, mets `GABFOOT_PUBLIC_URL=https://ton-domaine` dans Render;
7. verifie ensuite le domaine dans Render.

Pour les tests locaux hors Render, tu peux aussi memoriser une URL publique preferee:

```bash
bash /home/jibril/gabfoot-web-upload-20260316/set_preferred_public_url.sh https://gabfoot.eu.org
```

Suggestion de nom:

```text
gabfoot.eu.org
```

Alternatives si deja pris:

```text
appgabfoot.eu.org
gabfootpro.eu.org
gabfootlive.eu.org
```

Le site genere alors ses URLs publiques propres pour:
- la balise canonique
- `robots.txt`
- `sitemap.xml`
- les apercus de partage

Cette interface permet de:
- voir les matchs les plus surs
- voir l'affiche image actuelle
- voir une section `Pronostic Botola Pro` pour la premiere ligue marocaine
- voir une section `Tennis World` pour les matchs ATP/WTA
- changer le seuil et le nombre de matchs
- envoyer l'affiche directement sur Telegram

## Rapports Telegram automatiques

Le service principal peut maintenant:
- envoyer les affiches de pronostics toutes les `3 heures`
- verifier les resultats reels des matchs envoyes
- envoyer un `rapport horaire` avec le taux de reussite
- envoyer un `rapport quotidien` avec le pourcentage global de la journee
- envoyer un `rapport hebdomadaire` avec le pourcentage global sur 7 jours

Lancement:

```bash
bash /home/jibril/gabfoot-web-upload-20260316/start_notifications.sh
```

Arret:

```bash
bash /home/jibril/gabfoot-web-upload-20260316/stop_notifications.sh
```

## Lien public auto

Pour relancer automatiquement le backend + le tunnel public + l'envoi du nouveau lien sur Telegram:

```bash
bash /home/jibril/gabfoot-web-upload-20260316/start_public_app.sh
```

Pour arreter:

```bash
bash /home/jibril/gabfoot-web-upload-20260316/stop_public_app.sh
```

## Telegram

Renseigne `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` dans `.env`.

## Notes

- Le script construit un index local des equipes a partir des ligues populaires de FotMob.
- Si une equipe n'est pas trouvee, essaye son nom complet.
