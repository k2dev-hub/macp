#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/lib/config.sh"
BACKUP_ROOT="${MACP_BACKUP_ROOT:-$(cfg_path deployment.state_dir)/backups}"
KEEP_DAYS="${MACP_BACKUP_KEEP_DAYS:-$(cfg backup.retention_days 14)}"
BASE="${1:-$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)}"
if [ -f "$(dirname "$0")/.env" ]; then
  set -a; . "$(dirname "$0")/.env"; set +a
fi
DB_CONTAINER="macp-${MACP_DB_HOST:-db}"
mkdir -p "$BASE"

# The database may live on another host (deployment split: app here, DB there).
# In that case dump it over the network instead of `docker exec`-ing a local
# container; PG_DUMP_HOST/PORT come from the same [database] settings the API
# uses, and the tunnel/forward is the deployment's responsibility.
DB_REMOTE=0
if ! docker inspect "$DB_CONTAINER" >/dev/null 2>&1; then
  DB_REMOTE=1
  PG_HOST="${MACP_DB_HOST:-$(cfg database.host db)}"
  PG_PORT="${MACP_DB_PORT:-$(cfg database.port 5432)}"
  PG_IMAGE="${MACP_BACKUP_PG_IMAGE:-postgres:16-alpine}"
fi

# Run psql/pg_dump either locally in the db container or in a client container.
pg_exec() {
  if [ "$DB_REMOTE" = "1" ]; then
    docker run --rm -i --network "${MACP_BACKUP_DB_NETWORK:-macp-net}" "$PG_IMAGE" "$@"
  else
    docker exec -i "$DB_CONTAINER" "$@"
  fi
}

# Pause writers so the Git worktrees and PostgreSQL are captured at a stable
# point.  The API stays up for reads; nginx/db are untouched.
PAUSED=()
resume() {
  for name in "${PAUSED[@]:-}"; do
    [ -z "$name" ] && continue
    docker unpause "$name" >/dev/null 2>&1 || true
  done
}
trap resume EXIT

for name in $(docker ps --filter "name=macp-" --filter "status=running" --format '{{.Names}}' \
  | grep -E '^macp-(scheduler|coordinator|merge-worker|observer|worker-)' || true); do
  docker pause "$name" >/dev/null 2>&1 && PAUSED+=("$name")
done

if [ "$DB_REMOTE" = "1" ]; then
  pg_exec pg_dump -h "$PG_HOST" -p "$PG_PORT" -U macp -d macp -Fc > "$BASE/macp.dump"
else
  docker exec "$DB_CONTAINER" pg_dump -U macp -d macp -Fc > "$BASE/macp.dump"
fi

docker exec macp-api sh -c 'rm -rf /tmp/macp-bundles && mkdir -p /tmp/macp-bundles && git -C /app/workspace/repo.git bundle create /tmp/macp-bundles/demo.bundle --all && for repo in /app/workspace/projects/*/repo.git; do [ -d "$repo" ] || continue; name=$(basename "$(dirname "$repo")"); git -C "$repo" bundle create "/tmp/macp-bundles/$name.bundle" --all; done && tar -czf /tmp/macp-bundles.tgz -C /tmp/macp-bundles .'
docker cp macp-api:/tmp/macp-bundles.tgz "$BASE/bundles.tgz"

docker exec macp-api sh -c 'mkdir -p /app/workspace/tasks /app/workspace/merge /app/workspace/projects && tar -czf /tmp/macp-tasks.tgz -C /app/workspace tasks merge projects'
docker cp macp-api:/tmp/macp-tasks.tgz "$BASE/workspaces.tgz"

COUNTS_SQL="select 'tasks='||count(*) from tasks; select 'events='||count(*) from events; select 'receipts='||count(*) from receipts;"
if [ "$DB_REMOTE" = "1" ]; then
  counts="$(pg_exec psql -h "$PG_HOST" -p "$PG_PORT" -U macp -d macp -Atc "$COUNTS_SQL")"
else
  counts="$(docker exec "$DB_CONTAINER" psql -U macp -d macp -Atc "$COUNTS_SQL")"
fi
head="$(docker exec macp-api git -C /app/workspace/repo.git rev-parse main)"
cat > "$BASE/manifest.txt" <<EOF
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
main=$head
$counts
EOF

(
  cd "$BASE"
  sha256sum macp.dump bundles.tgz workspaces.tgz manifest.txt > SHA256SUMS
)

resume
trap - EXIT

if [ -d "$BACKUP_ROOT" ]; then
  find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + || true
fi

# Optional offsite copy on the NAS mount.  Keep 30 days there.
REMOTE="${MACP_BACKUP_REMOTE:-}"
if [ -z "$REMOTE" ] && cfg_enabled backup && [ -n "$(cfg backup.root "")" ]; then
  REMOTE="$(cfg backup.root "")/$(cfg backup.archive_dir "_archive/macp-backups")"
fi
if [ -d "$(dirname "$REMOTE")" ]; then
  mkdir -p "$REMOTE"
  cp -a "$BASE" "$REMOTE/"
  find "$REMOTE" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + || true
  (cd "$REMOTE/$(basename "$BASE")" && sha256sum -c SHA256SUMS >/dev/null)     && echo "offsite=$REMOTE/$(basename "$BASE")"
fi

echo "backup=$BASE"
cat "$BASE/SHA256SUMS"
