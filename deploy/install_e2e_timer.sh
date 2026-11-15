#!/usr/bin/env bash
# Monthly browser E2E on the server itself, then reset the baseline.
#
# Runs on day 1 at 05:30, after the restore drill (04:00) and the PITR drill
# (05:00), so a failure there is not masked by the E2E's task churn.
#
# ExecStartPost (the reset) only runs when the E2E succeeded: on failure the
# created task and its events are left in place for inspection.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"          # /opt/macp/deploy
REPO="$(cd "${DIR}/.." && pwd)"                # /opt/macp (systemd needs a normalised path)
E2E_RUN="${DIR}/e2e/run.sh"
chmod +x "$E2E_RUN" 2>/dev/null || true

cat > /etc/systemd/system/macp-e2e.service <<EOF
[Unit]
Description=MACP release verification (three toolchains + browser E2E) + baseline reset
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${REPO}
# Full release check: preflight, linux/android/windows build probes, browser E2E.
ExecStart=${DIR}/release-check.sh --keep
# Reset only when everything passed (release-check exits non-zero on any failure).
ExecStartPost=${DIR}/reset_test_data.sh --yes
TimeoutStartSec=3600
StandardOutput=append:/var/log/macp-e2e.log
StandardError=append:/var/log/macp-e2e.log
Environment=MACP_E2E_SERVER=1
EOF

cat > /etc/systemd/system/macp-e2e.timer <<'EOF'
[Unit]
Description=MACP monthly browser E2E

[Timer]
OnCalendar=*-*-01 05:30:00
AccuracySec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now macp-e2e.timer
systemctl list-timers 'macp-e2e.timer' --no-pager
