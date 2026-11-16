#!/usr/bin/env bash
# Weekly retention / archiving timer.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "${DIR}/retention.sh"

cat > /etc/systemd/system/macp-retention.service <<EOF
[Unit]
Description=MACP event archiving and derived-table retention
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=${DIR}
Environment=MACP_EVENTS_RETENTION_DAYS=90
ExecStart=${DIR}/retention.sh
EOF

cat > /etc/systemd/system/macp-retention.timer <<'EOF'
[Unit]
Description=MACP weekly retention run

[Timer]
OnCalendar=Sun *-*-* 04:30:00
AccuracySec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now macp-retention.timer
systemctl list-timers macp-retention.timer --no-pager
