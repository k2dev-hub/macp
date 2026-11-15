#!/usr/bin/env bash
# Cross-machine standby on the KVM host (build-host.example.com).
#
# Topology (after the 2026-11-15 audit):
#   .10  macp-db-standby (primary, published on 127.0.0.1:15432 only)
#        ^
#        |  SSH local-forward 15432 -> 127.0.0.1:15432  (macp-pg-tunnel.service)
#        |
#   .11  macp-remote-standby: bridge network, published on 127.0.0.1:15433 ONLY.
#
# The standby used to run with --network host, so PostgreSQL listened on
# 0.0.0.0:5432; together with the "host all all all trust" line that
# pg_basebackup copies from the primary, that exposed the entire database to the
# LAN without authentication.  This script pins it to loopback, rewrites
# pg_hba.conf, and fails if the exposure is still present.
#
#   ./remote_standby_setup.sh            # (re)create the standby
#   ./remote_standby_setup.sh --verify   # health + exposure checks only
set -euo pipefail

DATA_DIR=/opt/macp-remote-standby/data
CONTAINER=macp-remote-standby
TUNNEL_PORT=15432          # local forward to the primary (macp-pg-tunnel)
STANDBY_PORT=15433         # loopback publish for the standby itself
ROLE="${1:-setup}"

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

ensure_tunnel() {
  if ! systemctl is-active --quiet macp-pg-tunnel.service; then
    say "starting SSH tunnel unit"
    systemctl start macp-pg-tunnel.service
  fi
  for _ in $(seq 1 15); do
    if ss -lnt | grep -q "127.0.0.1:${TUNNEL_PORT}"; then
      return 0
    fi
    sleep 1
  done
  echo "tunnel did not come up (see systemctl status macp-pg-tunnel)" >&2
  exit 1
}

# Loopback only. 172.16.0.0/12 covers the docker bridge, which is reachable only
# from this host and is what docker-proxy uses for the 127.0.0.1 publish.
harden_hba() {
  local hba="$DATA_DIR/pg_hba.conf"
  say "rewriting ${hba} (loopback only, no trust-for-all)"
  cat >"$hba" <<'EOF'
# Managed by deploy/remote-standby/remote_standby_setup.sh - loopback only.
local   all         all                                 trust
host    all         all         127.0.0.1/32            trust
host    all         all         ::1/128                 trust
host    all         all         172.16.0.0/12           trust
local   replication all                                 trust
host    replication all         127.0.0.1/32            trust
host    replication all         ::1/128                 trust
EOF
  chown 70:70 "$hba" 2>/dev/null || true
  chmod 600 "$hba" 2>/dev/null || true
}

verify() {
  local rc=0
  say "container state: $(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
  local rec
  rec="$(docker exec "$CONTAINER" psql -U macp -d macp -Atc 'select pg_is_in_recovery()' 2>/dev/null | tr -d '[:space:]')"
  say "pg_is_in_recovery=${rec:-unknown}"
  [ "$rec" = "t" ] || { say "FAIL: standby is not in recovery"; rc=1; }

  if ss -lnt | awk '{print $4}' | grep -qE '^(0\.0\.0\.0|\*|\[::\]):5432$'; then
    say "FAIL: 5432 still listening on a wildcard address"
    ss -lntp | grep 5432 || true
    rc=1
  else
    say "OK: no wildcard 5432 listener"
  fi

  local la
  la="$(docker exec "$CONTAINER" psql -U macp -d macp -Atc 'show listen_addresses' 2>/dev/null | tr -d '[:space:]')"
  if [ "$la" = "127.0.0.1" ] || [ "$la" = "localhost" ]; then
    say "OK: listen_addresses=$la"
  else
    say "FAIL: listen_addresses=$la (expected 127.0.0.1)"
    rc=1
  fi

  local hba
  hba="$(docker exec "$CONTAINER" sh -c 'grep -cE "^host +all +all +all +trust" /data/pg_hba.conf || true' | tr -d '[:space:]')"
  if [ "${hba:-0}" != "0" ]; then
    say "FAIL: pg_hba still contains trust-for-all"
    rc=1
  else
    say "OK: no trust-for-all in pg_hba"
  fi
  return "$rc"
}

if [ "$ROLE" = "--verify" ]; then
  verify
  exit $?
fi

ensure_tunnel
mkdir -p "$DATA_DIR"
chmod 700 /opt/macp-remote-standby

say "stopping old standby"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
rm -rf "${DATA_DIR:?}"/*

say "pg_basebackup over the tunnel"
docker run --rm --network host -v "$DATA_DIR":/data postgres:16-alpine sh -c \
  "chown -R postgres:postgres /data && gosu postgres pg_basebackup -h 127.0.0.1 -p ${TUNNEL_PORT} -U macp -D /data -R -X stream -c fast"

# The base backup carries the primary's pg_hba (trust for all); replace it before
# the server starts.
harden_hba

say "starting standby (host network, PostgreSQL bound to 127.0.0.1 only)"
# Host network is kept because primary_conninfo points at 127.0.0.1:${TUNNEL_PORT},
# i.e. the SSH tunnel on the host loopback; a bridge network would make 127.0.0.1
# mean "the container" and replication would silently stop.  Exposure is closed by
# listen_addresses + pg_hba instead: PostgreSQL only binds host loopback.
docker run -d --name "$CONTAINER" \
  --network host \
  -v "$DATA_DIR":/data -e PGDATA=/data postgres:16-alpine \
  -c listen_addresses=127.0.0.1 >/dev/null

for _ in $(seq 1 25); do
  sleep 2
  if docker exec "$CONTAINER" psql -U macp -d macp -Atc "select 1" >/dev/null 2>&1; then
    break
  fi
done

verify || { echo "standby verification failed" >&2; exit 1; }
say "standby ready: verify on the primary that pg_stat_replication shows 2 clients"
