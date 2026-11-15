#!/usr/bin/env bash
# Retention / archiving for MACP's append-only tables.
#
#   ./deploy/retention.sh            # archive events, prune derived tables
#   ./deploy/retention.sh --dry-run  # show what would be removed
#
# Rules:
#   * `events` is the audit source of truth.  It is ARCHIVED to the NAS as
#     NDJSON and only deleted when MACP_EVENTS_RETENTION_DAYS is set AND the
#     archive for that range verified.  Prefix pruning is safe for
#     /v1/audit/chain because it only compares adjacent rows inside the window,
#     and anchors pin the deleted range's head hash.
#   * `alerts`, `metrics_daily`, `observer_dedupe`, `observer_notifications`
#     are derived: pruned on a fixed schedule.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/.."
. "$DIR/lib/config.sh"

ARCHIVE_DIR="${MACP_EVENT_ARCHIVE_DIR:-$(cfg audit.events_archive_dir "")}"
if [ -z "$ARCHIVE_DIR" ]; then
  ARCHIVE_DIR="$(cfg_path deployment.state_dir)/events-archive"
fi
EVENTS_DAYS="${MACP_EVENTS_RETENTION_DAYS:-0}"   # 0 = archive only, never delete
WINDOW_DAYS="${MACP_ARCHIVE_WINDOW_DAYS:-90}"    # rolling archive window when retention is off
ALERTS_DAYS="${MACP_ALERTS_RETENTION_DAYS:-30}"
METRICS_DAYS="${MACP_METRICS_RETENTION_DAYS:-400}"
DEDUPE_DAYS="${MACP_DEDUPE_RETENTION_DAYS:-7}"
NOTIFY_DAYS="${MACP_NOTIFY_RETENTION_DAYS:-30}"

if [ -f deploy/.env ]; then
  set -a; . deploy/.env; set +a
fi
DB_CONTAINER="macp-${MACP_DB_HOST:-db}"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

q() { docker exec -i "$DB_CONTAINER" psql -U macp -d macp -Atc "$1"; }
say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

say "retention start (dry_run=$DRY events_days=$EVENTS_DAYS)"

# ---------------------------------------------------------------- archive
archived_total=0
for p in $(q "select code from projects order by code"); do
  cutoff_days="$EVENTS_DAYS"
  # Archive everything older than the retention window; when retention is off,
  # archive a rolling 90-day window so the NAS copy is always warm.
  [ "$cutoff_days" = 0 ] && cutoff_days="$WINDOW_DAYS"
  oldest="$(q "select coalesce(min(seq),0) from events e join projects p on p.id=e.project_id
               where p.code='$p' and e.occurred_at < now() - interval '$cutoff_days days'")"
  newest="$(q "select coalesce(max(seq),0) from events e join projects p on p.id=e.project_id
               where p.code='$p' and e.occurred_at < now() - interval '$cutoff_days days'")"
  if [ "${newest:-0}" = 0 ] || [ "${oldest:-0}" = 0 ]; then
    say "$p: nothing older than ${cutoff_days}d to archive"
    continue
  fi
  count=$(( newest - oldest + 1 ))
  if [ "$DRY" = 1 ]; then
    say "$p: WOULD archive seq $oldest..$newest ($count rows expected)"
    continue
  fi
  month="$(date -u +%Y%m)"
  mkdir -p "$ARCHIVE_DIR/$month"
  out="$ARCHIVE_DIR/$month/events-$p-$oldest-$newest.ndjson.gz"
  docker exec -i "$DB_CONTAINER" psql -U macp -d macp -Atc \
    "select row_to_json(e) from events e join projects p on p.id=e.project_id
     where p.code='$p' and e.seq between $oldest and $newest order by e.seq" \
    | gzip -c >"$out"
  lines="$(zcat "$out" | wc -l)"
  if [ "$lines" -ne "$count" ]; then
    say "$p: ARCHIVE MISMATCH expected $count got $lines, refusing to prune ($out)"
    continue
  fi
  say "$p: archived seq $oldest..$newest ($lines rows) -> $out"
  archived_total=$(( archived_total + lines ))

  if [ "$EVENTS_DAYS" != 0 ]; then
    # outbox / observer_dedupe / observer_notifications reference events(id)
    # with ON DELETE RESTRICT, so drop the dependants for that range first.
    q "delete from observer_notifications n using events e, projects p
       where n.event_id=e.id and p.id=e.project_id and p.code='$p'
         and e.seq between $oldest and $newest" >/dev/null
    q "delete from observer_dedupe d using events e, projects p
       where d.event_id=e.id and p.id=e.project_id and p.code='$p'
         and e.seq between $oldest and $newest" >/dev/null
    q "delete from outbox o using events e, projects p
       where o.event_id=e.id and p.id=e.project_id and p.code='$p'
         and e.seq between $oldest and $newest" >/dev/null
    deleted="$(q "with del as (
                    delete from events e using projects p
                    where p.id=e.project_id and p.code='$p'
                      and e.seq between $oldest and $newest
                      and e.occurred_at < now() - interval '$EVENTS_DAYS days'
                    returning 1)
                  select count(*) from del" | tr -d '[:space:]')"
    say "$p: pruned $deleted events (retention ${EVENTS_DAYS}d)"
  fi
done

# ---------------------------------------------------------------- prune derived
prune() {
  local table="$1" where="$2" label="$3"
  if [ "$DRY" = 1 ]; then
    say "WOULD prune $label: $(q "select count(*) from $table where $where" | tr -d '[:space:]') rows"
    return
  fi
  local n
  n="$(q "with del as (delete from $table where $where returning 1) select count(*) from del" | tr -d '[:space:]')"
  say "pruned $label: $n rows"
}

prune alerts "state <> 'open' and last_seen < now() - interval '${ALERTS_DAYS} days'" "alerts>${ALERTS_DAYS}d"
prune metrics_daily "day < current_date - ${METRICS_DAYS}" "metrics_daily>${METRICS_DAYS}d"
prune observer_dedupe "sent_at < now() - interval '${DEDUPE_DAYS} days'" "observer_dedupe>${DEDUPE_DAYS}d"
prune observer_notifications "sent_at < now() - interval '${NOTIFY_DAYS} days'" "observer_notifications>${NOTIFY_DAYS}d"

say "retention done (archived_rows=$archived_total)"
