#!/usr/bin/env bash
# One-shot upgrade for the MACP deployment on macp.example.com.
#
#   ./deploy/upgrade.sh              # backup -> pull -> build all app images -> recreate -> nginx -> smoke
#   ./deploy/upgrade.sh --no-backup  # skip the pre-flight backup (not recommended)
#   ./deploy/upgrade.sh --force      # rebuild even when the commit did not change
#
# Background: every application service (api, api-2, observer, scheduler,
# coordinator, merge-worker, worker-*) is built into its OWN image tag from the
# same Dockerfile, so rebuilding only "api api-2" leaves the background services
# running old code (this bit us on 2026-11-14 when fixing the observer).
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f deploy/docker-compose.yml)
APP_SERVICES=(
  api api-2 signer observer scheduler coordinator merge-worker mock-openai oidc runner
  worker-deepseek worker-e worker-f worker-g worker-llm
)

BACKUP=1
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --no-backup) BACKUP=0 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" != "0" ]; then
  echo "upgrade.sh must run as root (docker + git credentials)" >&2
  exit 1
fi

# nginx publishes :9443 on one specific host address (not loopback, and not
# necessarily the first address of `hostname -I`), so ask compose which one.
if [ -n "${MACP_SMOKE_URL:-}" ]; then
  API_URL="$MACP_SMOKE_URL"
elif PORT_MAP="$("${COMPOSE[@]}" port nginx 443 2>/dev/null | head -1)" && [ -n "$PORT_MAP" ]; then
  API_URL="https://${PORT_MAP}"
else
  API_URL="https://$(hostname -I | awk '{print $1}'):9443"
fi

say() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }

say "smoke target: $API_URL"

old_head="$(git rev-parse --short HEAD)"
say "current HEAD: $old_head"

if [ "$BACKUP" = 1 ]; then
  say "pre-flight backup..."
  if ! ./deploy/backup.sh >/tmp/macp-upgrade-backup.log 2>&1; then
    echo "backup FAILED, aborting. Last lines:" >&2
    tail -20 /tmp/macp-upgrade-backup.log >&2
    exit 1
  fi
  tail -2 /tmp/macp-upgrade-backup.log
fi

say "git pull..."
git pull --ff-only
new_head="$(git rev-parse --short HEAD)"
say "new HEAD: $new_head"
if [ "$new_head" = "$old_head" ] && [ "$FORCE" = 0 ]; then
  say "no code change ($old_head); nothing to do (use --force to rebuild anyway)"
  exit 0
fi

say "building images for: ${APP_SERVICES[*]}"
"${COMPOSE[@]}" build "${APP_SERVICES[@]}"

say "recreating containers..."
"${COMPOSE[@]}" up -d --force-recreate "${APP_SERVICES[@]}"

say "restarting nginx (it caches upstream IPs)..."
"${COMPOSE[@]}" restart nginx >/dev/null

say "waiting for the API to become healthy..."
healthy=0
for _ in $(seq 1 40); do
  if curl -skf --max-time 3 "$API_URL/v1/health" | grep -q '"status":"ok"'; then
    healthy=1
    break
  fi
  sleep 3
done
if [ "$healthy" != 1 ]; then
  echo "smoke FAILED: /v1/health never became ok" >&2
  docker logs --tail 30 macp-api >&2 || true
  exit 1
fi

say "smoke checks:"
curl -sk "$API_URL/v1/version"; echo
curl -sk "$API_URL/v1/health"; echo
chain="$(curl -sk --max-time 10 "$API_URL/v1/audit/chain?limit=200")"
echo "$chain"
if ! echo "$chain" | grep -q '"ok":true'; then
  echo "smoke FAILED: event chain reports broken links" >&2
  exit 1
fi

not_up="$(docker ps -a --filter name=macp- --format '{{.Names}} {{.Status}}' \
  | grep -Ev ' Up ' | grep -Ev 'macp-db |macp-simulator' || true)"
if [ -n "$not_up" ]; then
  echo "WARNING: containers not running:" >&2
  echo "$not_up" >&2
fi

say "upgrade complete: $old_head -> $new_head"
