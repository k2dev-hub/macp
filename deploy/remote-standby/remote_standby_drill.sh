#!/usr/bin/env bash
# Cross-machine failover drill: promote the remote standby, prove it accepts
# writes, then re-clone it so the topology is healthy again.
#
# Run on build-host.example.com as root.  Safe: the remote standby is a separate copy; the
# live primary on .10 keeps serving MACP throughout.
set -euo pipefail

CONTAINER=macp-remote-standby
say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

say "state before promotion"
docker exec "$CONTAINER" psql -U macp -d macp -Atc "select pg_is_in_recovery()"

say "promoting"
docker exec "$CONTAINER" gosu postgres pg_ctl promote -D /data >/dev/null
for _ in $(seq 1 20); do
  sleep 1
  if docker exec "$CONTAINER" psql -U macp -d macp -Atc "select pg_is_in_recovery()" | grep -q f; then
    break
  fi
done
docker exec "$CONTAINER" psql -U macp -d macp -Atc "select 'in_recovery='||pg_is_in_recovery()"

say "write test on the promoted node"
docker exec "$CONTAINER" psql -U macp -d macp -c \
  "create table if not exists ha_drill(id serial primary key, note text, at timestamptz default now())" >/dev/null
docker exec "$CONTAINER" psql -U macp -d macp -c \
  "insert into ha_drill(note) values('promoted') returning id, note"

say "re-cloning as standby"
"$(dirname "$0")/remote_standby_setup.sh"

say "drill complete: promoted + wrote + rejoined as standby"
