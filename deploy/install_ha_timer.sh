#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cat > /etc/systemd/system/macp-pg-failover.service <<SERVICE
[Unit]
Description=MACP PostgreSQL failover
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
WorkingDirectory=${DIR}
ExecStart=${DIR}/pg_failover.sh
SERVICE
cat > /etc/systemd/system/macp-pg-failover.timer <<'TIMER'
[Unit]
Description=Check MACP PostgreSQL primary
[Timer]
OnCalendar=*:0/1
AccuracySec=5s
Persistent=true
[Install]
WantedBy=timers.target
TIMER
systemctl daemon-reload
systemctl enable --now macp-pg-failover.timer
systemctl list-timers macp-pg-failover.timer --no-pager
