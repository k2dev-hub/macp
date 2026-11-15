#!/usr/bin/env bash
# Daily audit anchor timer (runs before the 03:30 backup so the anchor is
# included in that night's dump).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "${DIR}/anchor_events.sh"

cat > /etc/systemd/system/macp-anchor.service <<EOF
[Unit]
Description=Pin MACP event-chain heads to audit_anchors and the NAS
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=${DIR}
ExecStart=${DIR}/anchor_events.sh
EOF

cat > /etc/systemd/system/macp-anchor.timer <<'EOF'
[Unit]
Description=MACP daily audit anchor

[Timer]
OnCalendar=*-*-* 03:20:00
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now macp-anchor.timer
systemctl list-timers macp-anchor.timer --no-pager
