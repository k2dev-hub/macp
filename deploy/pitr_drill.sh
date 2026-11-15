#!/usr/bin/env bash
# PITR drill: prove that the nightly dump is *not* the only recovery point.
#
#   1. take a base backup of the running primary,
#   2. insert two marker rows with a target timestamp between them,
#   3. switch WAL so the archive contains both,
#   4. start an isolated container that restores from the base backup and
#      replays archived WAL up to the target time,
#   5. assert that only the first marker is present.
#
# Everything happens in a scratch container; the live database is untouched.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/.."
. "$DIR/lib/config.sh"
. "$DIR/lib/db.sh"

if [ -f deploy/.env ]; then
  set -a; . deploy/.env; set +a
fi
DB_CONTAINER="macp-${MACP_DB_HOST:-db}"
ARCHIVE_DIR="${MACP_WAL_ARCHIVE_DIR:-$(cfg wal.host_archive_dir "")}"
NODE_DIR="$(basename "$DB_CONTAINER")"
TEST_CONTAINER="macp-pitr-drill"
BASE_IN_CONTAINER="/var/lib/postgresql/wal-archive/pitr-drill-base"

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
q() { psql_db -Atc "$1"; }

cleanup() {
  docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
  docker exec "$DB_CONTAINER" rm -rf "$BASE_IN_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

say "creating marker table"
q "create table if not exists pitr_drill_probe(id serial primary key, note text, at timestamptz default now())" >/dev/null
q "delete from pitr_drill_probe" >/dev/null

say "base backup of $DB_CONTAINER"
docker exec "$DB_CONTAINER" sh -c "rm -rf $BASE_IN_CONTAINER && pg_basebackup -D $BASE_IN_CONTAINER -Fp -Xs -c fast -U macp -h 127.0.0.1" >/dev/null

q "insert into pitr_drill_probe(note) values('before')" >/dev/null
target="$(q "select to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US')")"
say "recovery target: $target (UTC)"
sleep 2
q "insert into pitr_drill_probe(note) values('after')" >/dev/null
q "select pg_switch_wal()" >/dev/null
sleep 6

say "starting isolated restore container"
docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
# Local archive first, NAS second: wal_prune.sh deletes local segments only after
# verifying them on the NAS, so the drill must still work when local is short.
docker run -d --name "$TEST_CONTAINER" -e PGDATA=/pgdata \
  -v "$ARCHIVE_DIR":/wal:ro -v "${WAL_ARCHIVE_REMOTE:-/mnt/nas/_archive/macp-wal}":/wal-nas:ro \
  postgres:16-alpine sleep infinity >/dev/null
docker exec "$TEST_CONTAINER" sh -c \
  "rm -rf /pgdata && cp -a /wal/pitr-drill-base /pgdata && chown -R postgres:postgres /pgdata && chmod 700 /pgdata"
docker exec "$TEST_CONTAINER" sh -c \
  "printf '%s\n' \"restore_command = 'cp /wal/$NODE_DIR/%f %p || cp /wal-nas/$NODE_DIR/%f %p'\" \"recovery_target_time = '$target+00'\" \"recovery_target_action = 'promote'\" >> /pgdata/postgresql.auto.conf && touch /pgdata/recovery.signal"
docker exec -d "$TEST_CONTAINER" gosu postgres postgres -D /pgdata

say "waiting for recovery to finish"
ok=0
for _ in $(seq 1 30); do
  sleep 2
  if docker exec "$TEST_CONTAINER" psql -U macp -d macp -Atc "select 1" >/dev/null 2>&1; then
    ok=1
    break
  fi
done
if [ "$ok" != 1 ]; then
  docker logs --tail 20 "$TEST_CONTAINER" >&2 || true
  echo "pitr_drill=FAIL (restore did not come up)"
  exit 1
fi

rows="$(docker exec "$TEST_CONTAINER" psql -U macp -d macp -Atc \
  "select count(*)||':'||coalesce(string_agg(note,',' order by id),'') from pitr_drill_probe")"
say "recovered rows: $rows"
if [ "$rows" = "1:before" ]; then
  echo "pitr_drill=PASS (recovered exactly to the target time)"
  exit 0
fi
echo "pitr_drill=FAIL (expected 1:before, got $rows)"
exit 1
