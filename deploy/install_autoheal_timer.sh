#!/usr/bin/env bash
# Install a one-minute health watchdog for MACP containers.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cat > /etc/systemd/system/macp-autoheal.service <<EOF
[Unit]
Description=Restart unhealthy MACP containers
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=${DIR}
ExecStart=${DIR}/autoheal.sh
EOF

cat > /etc/systemd/system/macp-autoheal.timer <<'EOF'
[Unit]
Description=MACP container health watchdog

[Timer]
OnCalendar=*:0/1
AccuracySec=15s
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now macp-autoheal.timer
systemctl list-timers macp-autoheal.timer --no-pager
