#!/usr/bin/env bash
# Install the WAL archive sync timer (every 5 minutes) and a monthly PITR drill
# (day 1 at 05:00, after the 04:00 restore drill).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "${DIR}/wal_archive_sync.sh" "${DIR}/pitr_drill.sh"

cat > /etc/systemd/system/macp-wal-sync.service <<EOF
[Unit]
Description=Copy MACP archived WAL to the NAS
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=${DIR}
ExecStart=${DIR}/wal_archive_sync.sh
EOF

cat > /etc/systemd/system/macp-wal-sync.timer <<'EOF'
[Unit]
Description=MACP WAL archive offsite sync

[Timer]
OnCalendar=*:0/5
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/macp-pitr-drill.service <<EOF
[Unit]
Description=MACP point-in-time recovery drill
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=${DIR}
ExecStart=${DIR}/pitr_drill.sh
EOF

cat > /etc/systemd/system/macp-pitr-drill.timer <<'EOF'
[Unit]
Description=MACP monthly PITR drill

[Timer]
OnCalendar=*-*-01 05:00:00
AccuracySec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Local WAL pruning: archive_timeout forces a segment switch on a timer, so an
# idle database still grows the archive (~1GB/h at 60s). Deletes only segments
# whose NAS copy has been byte-verified.
chmod +x "${DIR}/wal_prune.sh"
cat > /etc/systemd/system/macp-wal-prune.service <<EOF
[Unit]
Description=Prune local WAL archive after verifying the NAS copy
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=${DIR}
ExecStart=${DIR}/wal_prune.sh
EOF

cat > /etc/systemd/system/macp-wal-prune.timer <<'EOF'
[Unit]
Description=MACP hourly local WAL prune

[Timer]
OnCalendar=hourly
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now macp-wal-sync.timer macp-pitr-drill.timer macp-wal-prune.timer
systemctl list-timers 'macp-wal-sync.timer' 'macp-pitr-drill.timer' 'macp-wal-prune.timer' --no-pager
