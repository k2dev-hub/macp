#!/usr/bin/env bash
# External dead-man switch for MACP.
#
# The observer is both the component that evaluates alerts and the component
# whose death would need alerting, so it cannot report its own death.  This
# script runs from systemd every minute, outside the container fleet, and:
#
#   1. checks the API and database health,
#   2. checks how long ago the observer last completed a scan
#      (observer.scanned, normally every 300s),
#   3. restarts macp-observer once when the scan is stale,
#   4. logs everything to /var/log/macp-watchdog.log and, when
#      MACP_ALERT_WEBHOOK is set, posts the failure there,
#   5. exits non-zero so systemd records the failure (macp-watchdog.service).
#
# Exit codes: 0 healthy, 1 degraded (restarted and recovered or minor issue),
# 2 critical (API/DB unreachable, or observer still stale after restart).
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

API="${MACP_WATCHDOG_URL:-}"
if [ -z "$API" ]; then
  port_map="$(docker compose -f docker-compose.yml port nginx 443 2>/dev/null | head -1)"
  if [ -n "$port_map" ]; then
    API="https://$port_map"
  else
    API="https://$(hostname -I | awk '{print $1}'):9443"
  fi
fi
LOG="${MACP_WATCHDOG_LOG:-/var/log/macp-watchdog.log}"
STALE_SECONDS="${MACP_WATCHDOG_STALE_SECONDS:-900}"
STATE_DIR="/var/lib/macp-watchdog"

if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
DB_CONTAINER="macp-${MACP_DB_HOST:-db}"

# The database may live on another host.  Detect that and run psql through a
# client container on the compose network instead of docker exec.
DB_REMOTE=0
if ! docker inspect "$DB_CONTAINER" >/dev/null 2>&1; then
  DB_REMOTE=1
  PG_HOST="${MACP_DB_HOST:-$(cfg database.host db)}"
  PG_PORT="${MACP_DB_PORT:-$(cfg database.port 5432)}"
  PG_IMAGE="${MACP_BACKUP_PG_IMAGE:-postgres:16-alpine}"
fi
psql_db() {
  if [ "$DB_REMOTE" = "1" ]; then
    docker run --rm -i --network "${MACP_BACKUP_DB_NETWORK:-macp-net}" "$PG_IMAGE" \
      psql -h "$PG_HOST" -p "$PG_PORT" -U macp -d macp "$@"
  else
    docker exec -i "$DB_CONTAINER" psql -U macp -d macp "$@"
  fi
}

mkdir -p "$STATE_DIR"
log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG"; }

# Planned maintenance (deploy/reset_test_data.sh stops services on purpose) sets
# this flag so the watchdog does not page for containers it asked to stop.
if [ -f "$STATE_DIR/maintenance" ]; then
  log "maintenance flag present, skipping checks"
  exit 0
fi

RC=0

# --- 1. API + DB -----------------------------------------------------------
health="$(curl -sk --max-time 5 "$API/v1/health" 2>/dev/null || true)"
if ! printf '%s' "$health" | grep -q '"status":"ok"'; then
  log "CRITICAL api health: ${health:-<no response>}"
  RC=2
elif ! printf '%s' "$health" | grep -q '"db":"ok"'; then
  log "CRITICAL api is up but database is not ok: $health"
  RC=2
fi

# --- 2. observer freshness -------------------------------------------------
scan_age() {
  psql_db -tAc \
    "select coalesce(extract(epoch from now()-max(occurred_at)),999999)::int
     from events where type='observer.scanned'" 2>/dev/null | tr -d '[:space:]'
}

age="$(scan_age)"
if [ -z "${age:-}" ]; then
  log "CRITICAL cannot read observer freshness (db=$DB_CONTAINER unreachable)"
  RC=2
elif [ "$age" -gt "$STALE_SECONDS" ]; then
  log "WARN observer scan is ${age}s old (threshold ${STALE_SECONDS}s), restarting macp-observer"
  docker restart macp-observer >/dev/null 2>&1 || log "ERROR docker restart macp-observer failed"
  sleep 25
  age="$(scan_age)"
  if [ -z "${age:-}" ] || [ "$age" -gt "$STALE_SECONDS" ]; then
    log "CRITICAL observer is still stale after restart (age=${age:-unknown}s)"
    RC=2
  else
    log "INFO observer recovered after restart (age=${age}s)"
    [ "$RC" = 0 ] && RC=1
  fi
fi

# --- 3. containers that must be running ------------------------------------
for c in macp-api macp-api-2 macp-nginx macp-observer macp-coordinator macp-scheduler macp-merge-worker; do
  running="$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || echo missing)"
  if [ "$running" != "true" ]; then
    log "CRITICAL container $c is not running (state=$running)"
    RC=2
  fi
done

# --- 3b. disk + WAL archive + archiver failures ---------------------------
# WAL segments are switched on a timer, so an idle database still grows the
# archive; without this check the root partition can fill silently.
FREE_MIN_GB="${MACP_DISK_MIN_FREE_GB:-15}"
free_gb="$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -n "${free_gb:-}" ] && [ "$free_gb" -lt "$FREE_MIN_GB" ]; then
  log "CRITICAL disk free ${free_gb}G < ${FREE_MIN_GB}G"
  RC=2
fi
wal_gb="$(du -sBG "${MACP_WAL_ARCHIVE_DIR:-/opt/macp/wal-archive}" 2>/dev/null | cut -f1 | tr -dc '0-9')"
wal_max_gb="${MACP_WAL_MAX_GB:-60}"
if [ -n "${wal_gb:-}" ] && [ "$wal_gb" -gt "$wal_max_gb" ]; then
  log "CRITICAL WAL archive ${wal_gb}G > ${wal_max_gb}G (wal_prune not keeping up?)"
  RC=2
fi
arch_failed="$(psql_db -tAc \
  "select failed_count from pg_stat_archiver" 2>/dev/null | tr -d '[:space:]')"
if [ -n "${arch_failed:-}" ] && [ "$arch_failed" != "0" ]; then
  log "CRITICAL pg_stat_archiver.failed_count=$arch_failed"
  RC=2
fi

# --- 4. webhook (optional) -------------------------------------------------
if [ "$RC" != 0 ] && [ -n "${MACP_ALERT_WEBHOOK:-}" ]; then
  payload="$(printf '{"source":"macp-watchdog","rc":%s,"host":"%s","health":%s}' \
    "$RC" "$(hostname)" "$(printf '%s' "${health:-null}" | sed 's/^$/null/')")"
  curl -sk --max-time 5 -X POST -H 'Content-Type: application/json' \
    -d "$payload" "$MACP_ALERT_WEBHOOK" >/dev/null 2>&1 \
    || log "WARN webhook post failed"
fi

# --- 5. state file + log hygiene ------------------------------------------
printf '%s rc=%s scan_age=%s\n' "$(date -Is)" "$RC" "${age:-unknown}" >"$STATE_DIR/state"
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5242880 ]; then
  tail -2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

if [ "$RC" != 0 ]; then
  echo "watchdog degraded: see $LOG" >&2
fi
exit "$RC"
