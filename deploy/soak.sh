#!/usr/bin/env bash
# Short soak + fault injection: load traffic while workers are restarted.
set -euo pipefail
DURATION="${1:-150}"
LOG="${MACP_SOAK_LOG:-/var/log/macp-soak.log}"

{
  echo "=== soak $(date -u +%FT%TZ) duration=${DURATION}s ==="
  docker exec macp-worker-e python -u -m agents.load_smoke > /tmp/macp-soak-load.log 2>&1 &
  LOAD_PID=$!
  for worker in worker-f worker-g worker-llm worker-deepseek; do
    sleep $((DURATION / 5))
    echo "fault: restart $worker $(date -u +%FT%TZ)"
    docker restart "macp-$worker" >/dev/null
  done
  wait "$LOAD_PID" || true
  echo "--- load ---"
  cat /tmp/macp-soak-load.log
  echo "--- final ---"
  curl -sk https://macp.example.com:9443/v1/overview | python3 -c 'import sys,json; d=json.load(sys.stdin); print({k:d[k] for k in ("execution_wip","review_debt","blocked","open_alerts")})'
  curl -sk "https://macp.example.com:9443/v1/audit/chain?limit=3000" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("audit", d["ok"], d["checked"], d["broken_links"])'
  docker ps --filter name=macp-worker --format '{{.Names}} {{.Status}}'
  echo "=== soak done $(date -u +%FT%TZ) ==="
} | tee -a "$LOG"
