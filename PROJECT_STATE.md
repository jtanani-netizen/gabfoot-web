# GABFOOT - Etat du projet

Date de sauvegarde: 2026-03-16

## Liens principaux

- Repo GitHub: `https://github.com/jtanani-netizen/gabfoot-web`
- Space Hugging Face: `https://huggingface.co/spaces/gabfootlive/app`
- Site en ligne: `https://gabfootlive-app.hf.space`
- Dossier local: `/home/jibril/gabfoot-web-upload-20260316`

## Etat actuel

- Deploiement actif sur Hugging Face Spaces en mode `docker`
- URL publique configuree: `https://gabfootlive-app.hf.space`
- Endpoints verifies le 2026-03-16:
  - `/`
  - `/dashboard`
  - `/articles`
  - `/healthz`
- Dernier commit local connu: `088d401`

## Fichiers importants de deploiement

- `Dockerfile`
- `.dockerignore`
- `.gitattributes`
- `README.md`
- `web_app.py`
- `.env.example`

## Variables utiles

- `GABFOOT_PUBLIC_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Pour reprendre le travail plus tard

1. Ouvrir le dossier local:
   `cd /home/jibril/gabfoot-web-upload-20260316`
2. Verifier l'etat Git:
   `git status`
3. Voir l'URL publique actuelle:
   `https://gabfootlive-app.hf.space`
4. Redemarrer localement si besoin:
   `bash /home/jibril/gabfoot-web-upload-20260316/start_web_app.sh`

## Notes

- Ne pas repartir de zero: ce repo GitHub est maintenant la source principale pour le deploiement web.
- Le Space a ete transfere de `tjibril1983/gabfoot-web` vers `gabfootlive/app` pour enlever le nom personnel de l'adresse publique.
- Si Telegram doit fonctionner sur le Space, ajouter `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` dans les settings du Space.
- Si les anciens tokens temporaires existent encore, les revoquer apres usage.
