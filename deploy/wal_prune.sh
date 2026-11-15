#!/usr/bin/env bash
# Prune locally archived WAL after proving the NAS copy is intact.
#
# Why: archive_timeout forces a 16MB segment switch on a timer even when the
# database is idle, so /opt/macp/wal-archive grows ~1GB/h with empty segments.
# Without a local retention policy that fills the root partition in ~3 days, and
# once archive_command starts failing PostgreSQL keeps pg_wal and can stop
# accepting writes.
#
# Safety rules (fail closed):
#   * never delete a segment the NAS does not already have, byte-identical;
#   * never delete anything newer than MACP_WAL_LOCAL_HOURS (default 36h);
#   * never delete the newest MACP_WAL_KEEP_SEGMENTS (default 64) segments;
#   * refuse to run when free space is already critical (nothing to gain).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/.."
. "$DIR/lib/config.sh"

LOCAL="${MACP_WAL_ARCHIVE_DIR:-$(cfg wal.host_archive_dir "")}"
NAS="${MACP_WAL_ARCHIVE_REMOTE:-$(cfg backup.wal_mirror_dir "")}"
if [ -n "$NAS" ] && [ -n "$(cfg backup.root "")" ]; then
  NAS="$(cfg backup.root "")/$NAS"
fi
HOURS="${MACP_WAL_LOCAL_HOURS:-36}"
KEEP="${MACP_WAL_KEEP_SEGMENTS:-64}"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

[ -d "$LOCAL" ] || { say "no local archive at $LOCAL"; exit 0; }
if [ ! -d "$(dirname "$NAS")" ]; then
  say "NAS not mounted ($(dirname "$NAS")), refusing to delete anything"
  exit 1
fi

total_freed=0
total_deleted=0

for node_dir in "$LOCAL"/*/; do
  [ -d "$node_dir" ] || continue
  node="$(basename "$node_dir")"
  nas_dir="$NAS/$node"
  [ -d "$nas_dir" ] || { say "$node: no NAS copy at $nas_dir, skipping"; continue; }

  # Oldest-last ordering so the newest KEEP segments are never touched.
  mapfile -t segments < <(find "$node_dir" -maxdepth 1 -type f -name '0*' -printf '%T@ %f\n' \
    | sort -n | awk '{print $2}')
  count="${#segments[@]}"
  if [ "$count" -eq 0 ]; then
    say "$node: no segments"
    continue
  fi

  limit=$(( count - KEEP ))
  deleted=0
  freed=0
  kept_recent=0
  missing=0

  for ((i = 0; i < limit; i++)); do
    seg="${segments[$i]}"
    src="$node_dir/$seg"
    # Skip anything modified inside the retention window.
    if [ -n "$(find "$src" -mmin -$(( HOURS * 60 )) -print -quit)" ]; then
      kept_recent=$((kept_recent + 1))
      continue
    fi
    dst="$nas_dir/$seg"
    if [ ! -f "$dst" ]; then
      missing=$((missing + 1))
      continue
    fi
    # Byte-identical check before deleting the only other copy.
    if ! cmp -s "$src" "$dst"; then
      say "$node: NAS copy of $seg differs, refusing to delete"
      exit 1
    fi
    size="$(stat -c%s "$src")"
    if [ "$DRY" = 1 ]; then
      deleted=$((deleted + 1)); freed=$((freed + size))
      continue
    fi
    rm -f "$src"
    deleted=$((deleted + 1)); freed=$((freed + size))
  done

  say "$node: segments=$count pruned=$deleted freed=$((freed / 1024 / 1024))MB kept_recent=$kept_recent nas_missing=$missing"
  total_deleted=$((total_deleted + deleted))
  total_freed=$((total_freed + freed))
done

# Never let the archive grow past the disk: warn loudly instead of deleting
# unverified data.
free_gb="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
say "local WAL total pruned=$total_deleted freed=$((total_freed / 1024 / 1024))MB free=${free_gb}G"
if [ "${free_gb:-0}" -lt 10 ]; then
  say "CRITICAL: free space below 10G after prune"
  exit 2
fi
exit 0
