#!/usr/bin/env bash
# Install a daily systemd timer for MACP backups.  Run on the Docker host.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cat > /etc/systemd/system/macp-backup.service <<EOF
[Unit]
Description=MACP PostgreSQL and Git repository backup
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=${DIR}
ExecStart=${DIR}/backup.sh
EOF

cat > /etc/systemd/system/macp-backup.timer <<'EOF'
[Unit]
Description=Daily MACP backup

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now macp-backup.timer
systemctl list-timers macp-backup.timer --no-pager
