#!/usr/bin/env bash
# Copy archived WAL to the NAS so the PITR window survives losing this host.
#
# Runs every 5 minutes.  Uses --append-verify style semantics (rsync default for
# existing partial files is to re-copy), keeps the archive directories as-is.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/.."
. "$DIR/lib/config.sh"

SRC="${MACP_WAL_ARCHIVE_DIR:-$(cfg wal.host_archive_dir "")}"
DST="${MACP_WAL_ARCHIVE_REMOTE:-$(cfg backup.wal_mirror_dir "")}"
if [ -n "$DST" ] && [ -n "$(cfg backup.root "")" ]; then
  DST="$(cfg backup.root "")/$DST"
fi
LOG="${MACP_WAL_SYNC_LOG:-/var/log/macp-wal-sync.log}"

log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; }

if [ ! -d "$SRC" ]; then
  log "no archive dir at $SRC, nothing to sync"
  exit 0
fi
if [ ! -d "$(dirname "$DST")" ]; then
  log "NAS mount missing ($(dirname "$DST")), skipping"
  exit 0
fi

mkdir -p "$DST"
# Never delete on the NAS side: it is the offsite copy of last resort.
# `cp -an` is used instead of rsync because the host has no rsync package and the
# archive is append-only (no-clobber is exactly the semantics we need).
mkdir -p "$DST/macp-db-standby" "$DST/macp-db-standby-2"
for node in macp-db-standby macp-db-standby-2; do
  [ -d "$SRC/$node" ] && cp -an "$SRC/$node/." "$DST/$node/" 2>/dev/null || true
done

files="$(find "$DST" -type f -name '0*' | wc -l)"
bytes="$(du -sh "$DST" | cut -f1)"
log "synced: $files WAL files, $bytes at $DST"

# Keep the log bounded.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 2097152 ]; then
  tail -2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
