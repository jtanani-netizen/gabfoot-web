#!/usr/bin/env bash
set -euo pipefail

# Paths
SERV_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERV_DIR"

cat > "$SERV_DIR/match_scan.service" <<'UNIT'
[Unit]
Description=Scan API-Football et envoi Telegram
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/jibril/match_analyzer
EnvironmentFile=/home/jibril/match_analyzer/.env
Environment=LEAGUES=61,39,140
Environment=NEXT_MATCHES=3
Environment=LAST_N=5
ExecStart=/home/jibril/match_analyzer/.venv/bin/python /home/jibril/match_analyzer/scan_fixtures.py
Restart=on-failure

[Install]
WantedBy=default.target
UNIT

cat > "$SERV_DIR/match_scan.timer" <<'UNIT'
[Unit]
Description=Lancer match_scan toutes les 15 minutes et au boot

[Timer]
OnBootSec=1min
OnUnitActiveSec=15min
Persistent=true
Unit=match_scan.service

[Install]
WantedBy=timers.target
UNIT

chmod +x /home/jibril/match_analyzer/scan_fixtures.py

systemctl --user daemon-reload
systemctl --user enable --now match_scan.timer

echo "Done. Check status with: systemctl --user status match_scan.timer"
