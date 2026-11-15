#!/usr/bin/env bash
# Promote the first healthy standby when the current primary is unavailable.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER="$DIR/.failed_over"
LOG="$DIR/failover.log"

if [ -f "$MARKER" ]; then
  exit 0
fi
if [ ! -f "$DIR/.env" ]; then
  touch "$DIR/.env"
fi
CURRENT=$(grep '^MACP_DB_HOST=' "$DIR/.env" | tail -1 | cut -d= -f2)
CURRENT=${CURRENT:-db}
if docker exec "macp-$CURRENT" pg_isready -U macp -d macp >/dev/null 2>&1; then
  # A previous failover can leave the old primary running with stale data (two
  # primaries = split brain).  Warn loudly instead of failing, since the live one
  # is healthy and nothing should touch the stale copy.
  primaries=0
  for container in macp-db macp-db-standby macp-db-standby-2; do
    role=$(docker exec "$container" psql -U macp -d macp -Atc "select pg_is_in_recovery()" 2>/dev/null || true)
    [ "$role" = "f" ] && primaries=$((primaries + 1))
  done
  if [ "$primaries" -gt 1 ]; then
    echo "$(date -u +%FT%TZ) WARNING: $primaries primaries running (macp-$CURRENT is the live one) — stop or re-clone the stale copy" >> "$LOG"
  fi
  exit 0
fi

for candidate in db-standby db-standby-2; do
  [ "$candidate" = "$CURRENT" ] && continue
  container="macp-$candidate"
  if ! docker exec "$container" pg_isready -U macp -d macp >/dev/null 2>&1; then
    continue
  fi
  recovery=$(docker exec "$container" psql -U macp -d macp -Atc "select pg_is_in_recovery()" 2>/dev/null || echo t)
  if [ "$recovery" = "t" ]; then
    docker exec -u postgres "$container" pg_ctl promote -D /var/lib/postgresql/data >> "$LOG" 2>&1
  fi
  for _ in $(seq 1 30); do
    if docker exec "$container" pg_isready -U macp -d macp >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  sed -i "s/^MACP_DB_HOST=.*/MACP_DB_HOST=$candidate/" "$DIR/.env" 2>/dev/null || echo "MACP_DB_HOST=$candidate" >> "$DIR/.env"
  docker update --restart=no "macp-$CURRENT" >/dev/null 2>&1 || true
  docker stop "macp-$CURRENT" >/dev/null 2>&1 || true
  ( cd "$DIR" && docker compose up -d --force-recreate api api-2 observer >> "$LOG" 2>&1 \
      && docker compose restart nginx >> "$LOG" 2>&1 )
  touch "$MARKER"
  echo "$(date -u +%FT%TZ) failover complete: $candidate promoted (was $CURRENT)" >> "$LOG"
  exit 0
done

echo "$(date -u +%FT%TZ) primary down, no standby ready" >> "$LOG"
exit 1
