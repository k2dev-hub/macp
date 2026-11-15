#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cat > /etc/systemd/system/macp-restore-drill.service <<SERVICE
[Unit]
Description=MACP cross-site restore drill
After=docker.service
[Service]
Type=oneshot
ExecStart=${DIR}/restore_drill.sh
SERVICE
cat > /etc/systemd/system/macp-restore-drill.timer <<'TIMER'
[Unit]
Description=Monthly MACP restore drill
[Timer]
OnCalendar=*-*-01 04:00:00
Persistent=true
[Install]
WantedBy=timers.target
TIMER
systemctl daemon-reload
systemctl enable --now macp-restore-drill.timer
systemctl list-timers macp-restore-drill.timer --no-pager
