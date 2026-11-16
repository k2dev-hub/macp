#!/usr/bin/env bash
# Remove synthetic/demo tasks and their event history. Run only after a backup.
set -euo pipefail

if [ "${1:-}" != "--yes" ]; then
  echo "usage: $0 --yes   # destroys task/event history, keeps projects/agents config"
  exit 1
fi

cd "$(dirname "$0")"
. lib/config.sh
. lib/db.sh
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# Stop the external watchdog from raising "container not running" for the
# services this script deliberately stops.
MAINT_DIR=/var/lib/macp-watchdog
mkdir -p "$MAINT_DIR"
touch "$MAINT_DIR/maintenance"
trap 'rm -f "$MAINT_DIR/maintenance"' EXIT

docker stop macp-scheduler macp-coordinator macp-merge-worker \
  macp-worker-e macp-worker-f macp-worker-g macp-worker-llm macp-worker-deepseek \
  macp-observer >/dev/null 2>&1 || true

docker exec macp-api sh -c 'rm -rf /app/workspace/tasks /app/workspace/merge /app/workspace/repo.git /app/workspace/projects'

# The audit anchors pin event-chain heads.  Truncating events without them leaves
# every stored anchor pointing at a row that no longer exists (ok:false, and
# anchor_events.sh --verify reports BROKEN).  Archive the anchors together with
# the chain they describe, drop the rows, and rotate the NAS file so the new
# baseline starts a fresh chain instead of inheriting a dangling one.
ANCHOR_DIR="${MACP_ANCHOR_DIR:-/mnt/nas/_archive/macp-anchors}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$ANCHOR_DIR/archive/baseline-$STAMP"
if psql_db -Atc "select count(*) from audit_anchors" 2>/dev/null | grep -qv '^0$'; then
  mkdir -p "$ARCHIVE"
  psql_db -Atc \
    "select to_char(a.day,'YYYY-MM-DD'), p.code, a.last_seq, a.hash, a.external_ref
       from audit_anchors a join projects p on p.id=a.project_id
      order by a.day, p.code" >"$ARCHIVE/audit_anchors.tsv" 2>/dev/null || true
  if [ -d "$ANCHOR_DIR" ]; then
    for f in "$ANCHOR_DIR"/anchors-*.log; do
      [ -f "$f" ] || continue
      mv "$f" "$ARCHIVE/" 2>/dev/null || true
    done
  fi
  psql_db -c "delete from audit_anchors" >/dev/null
  echo "archived audit anchors -> $ARCHIVE (chain starts fresh after this reset)"
fi

psql_db <<'SQL'
BEGIN;
TRUNCATE tasks CASCADE;
TRUNCATE events CASCADE;
TRUNCATE alerts;
TRUNCATE metrics_daily;
TRUNCATE evidence CASCADE;
TRUNCATE repos CASCADE;
UPDATE observer_cursors SET since_seq=0;
DELETE FROM projects WHERE code <> 'demo';
COMMIT;
SQL

docker compose up -d --force-recreate api api-2 >/dev/null
sleep 8
docker exec macp-api sh -c 'mkdir -p /app/workspace/tasks /app/workspace/merge /app/workspace/projects'
docker exec macp-api python -c 'from server.tool_registry import ensure_repo; ensure_repo()'
docker compose up -d scheduler coordinator merge-worker worker-e worker-f worker-g worker-llm worker-deepseek observer >/dev/null
docker compose restart nginx >/dev/null
rm -f "$MAINT_DIR/maintenance"
trap - EXIT
echo "reset complete"
