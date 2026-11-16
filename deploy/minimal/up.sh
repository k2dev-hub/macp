#!/usr/bin/env bash
# 最小审计版：一条命令起、一条命令验。
#
#   ./deploy/minimal/up.sh              # 起 11 个容器并自检
#   ./deploy/minimal/up.sh --check      # 只做自检
#   ./deploy/minimal/up.sh --down       # 停掉（保留数据卷）
#   ./deploy/minimal/up.sh --reset      # 停掉并删除数据卷
#
# 与完整版共用镜像、密钥和 agent 公钥，所以先在完整版跑过一次
# ./deploy/bootstrap.sh（或手动准备 deploy/secrets 与 deploy/agent_*）。
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY="$(cd "$DIR/.." && pwd)"
cd "$DIR"

BASE="${MACP_MIN_URL:-http://127.0.0.1:8280}"
TOKEN_FILE="$DEPLOY/secrets/write_token"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

compose() { docker compose -f docker-compose.yml "$@"; }

case "${1:-}" in
  --down)  compose down --remove-orphans; say "stopped"; exit 0 ;;
  --reset) compose down -v --remove-orphans; say "stopped and volumes removed"; exit 0 ;;
esac

for f in write_token signer_token runner_token; do
  [ -s "$DEPLOY/secrets/$f" ] || die "missing deploy/secrets/$f — run ./deploy/bootstrap.sh first"
done
[ -s "$DEPLOY/agent_pubkeys/worker-e.pub" ] || die "missing agent public keys — run ./deploy/bootstrap.sh first"

if [ "${1:-}" != "--check" ]; then
  say "starting the minimal edition"
  compose up -d --no-build db api signer runner nginx observer scheduler coordinator merge-worker worker-e mock-openai
fi

say "waiting for the API"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "$BASE/v1/health" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS --max-time 5 "$BASE/v1/health" >/dev/null || die "API did not become healthy at $BASE"
say "API healthy at $BASE"

TOKEN="$(cat "$TOKEN_FILE")"
api() { curl -fsS --max-time 30 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }

say "audit chain"
api "$BASE/v1/audit/summary" 2>/dev/null | head -c 300 || api "$BASE/v1/events?limit=1" >/dev/null
echo

say "containers"
docker ps --filter "name=macpmin-" --format "  {{.Names}}\t{{.Status}}"

say "memory"
docker stats --no-stream --format "  {{.Name}}\t{{.MemUsage}}" $(docker ps -q --filter "name=macpmin-") 2>/dev/null | head -12

cat <<EOF

最小版已就绪
  入口   $BASE
  令牌   $TOKEN_FILE（0600，不入库）
  停止   ./deploy/minimal/up.sh --down

下一步：浏览器打开 $BASE，用管理员令牌登录，
在「任务」里新建一个任务，跑一次 compile 门禁，观察事件链与回执。
EOF
