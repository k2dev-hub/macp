#!/usr/bin/env bash
# Run the browser E2E. Three supported modes:
#
#   A) server, installed Chromium (recommended; no downloads, laptop-independent):
#        ./deploy/e2e/run.sh --server
#      Uses /usr/bin/chromium (apt install chromium) through Playwright with
#      --no-zygote, which is what Debian's build needs to not SIGTRAP.
#
#   B) container with the Playwright image (self-contained but ~2GB):
#        ./deploy/e2e/run.sh
#
#   C) an installed branded browser on a workstation:
#        E2E_CHANNEL=msedge ./deploy/e2e/run.sh
#
# Screenshots land in $E2E_OUT (default deploy/e2e/out).
#
# The script creates a task, dispatches it through the console and waits for it
# to merge.  For a deterministic dispatch stop the scheduler and the LLM workers
# first (see docs/OPERATIONS-2026-11-15.md §9), and reset the baseline afterwards
# if you want a clean audit chain.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/../.."

. "$DIR/../lib/config.sh"
BASE_URL="${E2E_BASE:-$(cfg deployment.public_url "https://localhost:9443")}"
OUT="${E2E_OUT:-$DIR/out}"
IMAGE="${E2E_IMAGE:-mcr.microsoft.com/playwright/python:v1.65.2-jammy}"
VENV="${E2E_VENV:-/opt/macp-e2e/venv}"
mkdir -p "$OUT"

run_local() {
  local python_bin="$1"
  shift
  E2E_BASE="$BASE_URL" E2E_OUT="$OUT" "$@" "$python_bin" deploy/e2e/ui_e2e.py
}

if [ "${1:-}" = "--server" ]; then
  echo "mode: server chromium ($(command -v chromium || echo missing))"
  command -v chromium >/dev/null || { echo "install chromium first (apt-get install -y chromium)"; exit 1; }
  export E2E_EXECUTABLE="${E2E_EXECUTABLE:-/usr/bin/chromium}"
  # On the host the secrets are plain files, not /run/secrets.
  export E2E_TOKEN_FILE="${E2E_TOKEN_FILE:-/opt/macp/deploy/secrets/write_token}"
  export E2E_OIDC_PW_FILE="${E2E_OIDC_PW_FILE:-/opt/macp/deploy/secrets/oidc_admin_password}"

  # Deterministic dispatch has two parts:
  #  * the console batches onto whoever the roster reports as idle, and an
  #    idle-but-stopped LLM worker would swallow the task, so park it;
  #  * the scheduler would dispatch the fresh task before the UI can click
  #    "批量派单", which makes the button a no-op. Stop it for the run.
  DB="$(grep '^MACP_DB_HOST=' deploy/.env 2>/dev/null | tail -1 | cut -d= -f2)"
  DB_CONTAINER="macp-${DB:-db}"
  psql_q() { docker exec -i "$DB_CONTAINER" psql -U macp -d macp -Atc "$1"; }
  park() {
    psql_q "update agents set status='waiting_user' where code in ('worker-deepseek','worker-llm')" >/dev/null 2>&1 || true
    docker stop macp-scheduler >/dev/null 2>&1 || true
  }
  unpark() {
    psql_q "update agents set status='idle' where code in ('worker-deepseek','worker-llm') and status='waiting_user'" >/dev/null 2>&1 || true
    docker start macp-scheduler >/dev/null 2>&1 || true
  }
  trap unpark EXIT
  park
  echo "parked worker-deepseek/worker-llm and paused the scheduler (restored on exit)"

  if [ -x "$VENV/bin/python" ]; then
    E2E_BASE="$BASE_URL" E2E_OUT="$OUT" E2E_EXECUTABLE="$E2E_EXECUTABLE" \
      E2E_TOKEN_FILE="$E2E_TOKEN_FILE" E2E_OIDC_PW_FILE="$E2E_OIDC_PW_FILE" \
      "$VENV/bin/python" deploy/e2e/ui_e2e.py
  else
    E2E_BASE="$BASE_URL" E2E_OUT="$OUT" E2E_EXECUTABLE="$E2E_EXECUTABLE" \
      E2E_TOKEN_FILE="$E2E_TOKEN_FILE" E2E_OIDC_PW_FILE="$E2E_OIDC_PW_FILE" \
      python3 deploy/e2e/ui_e2e.py
  fi
  exit $?
fi

if [ -n "${E2E_CHANNEL:-}" ]; then
  echo "mode: installed browser channel=$E2E_CHANNEL"
  E2E_BASE="$BASE_URL" E2E_OUT="$OUT" E2E_CHANNEL="$E2E_CHANNEL" \
    python3 deploy/e2e/ui_e2e.py
  exit $?
fi

echo "mode: container $IMAGE"
docker run --rm --network host \
  -v "$DIR":/e2e:ro \
  -v "$OUT":/e2e/out \
  -v /opt/macp/deploy/secrets/write_token:/run/secrets/wtok:ro \
  -v /opt/macp/deploy/secrets/oidc_admin_password:/run/secrets/oidcpw:ro \
  -e E2E_BASE="$BASE_URL" -e E2E_OUT=/e2e/out \
  "$IMAGE" python /e2e/ui_e2e.py
