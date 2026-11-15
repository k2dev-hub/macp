#!/usr/bin/env bash
# Restart MACP containers that Docker marks unhealthy.  Run from a systemd timer.
set -euo pipefail

# macp-db is deliberately excluded: an unhealthy database needs investigation,
# not a blind restart.
mapfile -t unhealthy < <(
  docker ps \
    --filter "name=macp-" \
    --filter "health=unhealthy" \
    --format '{{.Names}}' \
    | grep -v '^macp-db$' || true
)

for name in "${unhealthy[@]:-}"; do
  [ -z "$name" ] && continue
  echo "$(date -u +%FT%TZ) restarting unhealthy $name"
  docker unpause "$name" >/dev/null 2>&1 || true
  docker restart "$name" >/dev/null
done
