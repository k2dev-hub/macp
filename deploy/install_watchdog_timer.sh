#!/usr/bin/env bash
# Install the MACP dead-man switch (external observer watchdog).
#
# Runs every minute, outside the container fleet, so it still fires when the
# observer itself is the thing that died.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "${DIR}/watchdog.sh"

cat > /etc/systemd/system/macp-watchdog.service <<EOF
[Unit]
Description=MACP external watchdog (API/DB/observer freshness)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=${DIR}
ExecStart=${DIR}/watchdog.sh
# A non-zero exit is meaningful (degraded/critical), keep it visible but do not
# let a flapping check look like a unit failure to systemd-timers.
SuccessExitStatus=1
EOF

cat > /etc/systemd/system/macp-watchdog.timer <<'EOF'
[Unit]
Description=MACP watchdog every minute

[Timer]
OnCalendar=*:0/1
AccuracySec=10s
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now macp-watchdog.timer
systemctl list-timers macp-watchdog.timer --no-pager
