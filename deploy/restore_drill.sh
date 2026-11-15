#!/usr/bin/env bash
# Monthly cross-site restore drill: verify checksums and restore the newest copy.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/lib/config.sh"
LOCAL_ROOT="${MACP_BACKUP_ROOT:-$(cfg_path deployment.state_dir)/backups}"
REMOTE_ROOT="${MACP_BACKUP_REMOTE:-}"
if [ -z "$REMOTE_ROOT" ] && cfg_enabled backup && [ -n "$(cfg backup.root "")" ]; then
  REMOTE_ROOT="$(cfg backup.root "")/$(cfg backup.archive_dir "_archive/macp-backups")"
fi
LOG="${MACP_DRILL_LOG:-/var/log/macp-restore-drill.log}"
LATEST_LOCAL=$(find "$LOCAL_ROOT" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)
LATEST_REMOTE=$(find "$REMOTE_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -1)
if [ -z "$LATEST_LOCAL" ]; then
  echo "no local backup" >&2
  exit 1
fi
{
  echo "=== $(date -u +%FT%TZ) drill ==="
  echo "local=$LATEST_LOCAL"
  echo "remote=${LATEST_REMOTE:-none}"
  /opt/macp/deploy/restore_verify.sh "$LATEST_LOCAL"
  if [ -n "$LATEST_REMOTE" ] && [ -d "$LATEST_REMOTE" ]; then
    (cd "$LATEST_REMOTE" && sha256sum -c SHA256SUMS)
  fi
  echo "drill=PASS"
} >> "$LOG" 2>&1
tail -6 "$LOG"
