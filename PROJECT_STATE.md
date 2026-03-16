# GABFOOT - Etat du projet

Date de sauvegarde: 2026-03-16

## Liens principaux

- Repo GitHub: `https://github.com/jtanani-netizen/gabfoot-web`
- Space Hugging Face: `https://huggingface.co/spaces/tjibril1983/gabfoot-web`
- Site en ligne: `https://tjibril1983-gabfoot-web.hf.space`
- Dossier local: `/home/jibril/gabfoot-web-upload-20260316`

## Etat actuel

- Deploiement actif sur Hugging Face Spaces en mode `docker`
- URL publique configuree: `https://tjibril1983-gabfoot-web.hf.space`
- Endpoints verifies le 2026-03-16:
  - `/`
  - `/dashboard`
  - `/articles`
  - `/healthz`
- Dernier commit local connu: `65fdac7`

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
   `https://tjibril1983-gabfoot-web.hf.space`
4. Redemarrer localement si besoin:
   `bash /home/jibril/gabfoot-web-upload-20260316/start_web_app.sh`

## Notes

- Ne pas repartir de zero: ce repo GitHub est maintenant la source principale pour le deploiement web.
- Si Telegram doit fonctionner sur le Space, ajouter `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` dans les settings du Space.
- Si les anciens tokens temporaires existent encore, les revoquer apres usage.
