#!/bin/bash
set -e
export PGDATA="${PGDATA:-/var/lib/postgresql/data}"
PRIMARY_HOST="${PRIMARY_HOST:-db}"
mkdir -p "$PGDATA"
chown -R postgres:postgres "$PGDATA"
chmod 700 "$PGDATA"
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  # Only a fresh cluster needs the seed node.  Waiting unconditionally would
  # deadlock a promoted primary whose seed node (the cold copy `db`) is stopped.
  until pg_isready -h "$PRIMARY_HOST" -U macp -d macp >/dev/null 2>&1; do
    sleep 2
  done
  echo "taking base backup from primary"
  rm -rf "${PGDATA:?}/"*
  gosu postgres pg_basebackup -h "$PRIMARY_HOST" -U macp -D "$PGDATA" -R -X stream -c fast
fi
# compose `command: [postgres, -c, ...]` keeps the program name as $1; drop it so
# the flags are not passed to postgres as a position argument.
if [ "${1:-}" = "postgres" ]; then
  shift
fi
exec gosu postgres postgres "$@"
