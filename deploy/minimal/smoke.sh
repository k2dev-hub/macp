#!/usr/bin/env bash
# 最小审计版的自证脚本：跑完一条完整的审计闭环，并逐项断言。
#
#   ./deploy/minimal/smoke.sh
#
# 闭环：建任务 → 派单 → 认领 → 写文件 → 门禁 → 提交候选 → 回执 → 验签 → 审计链
# 任何一步不符预期就以非零退出，用于演示前自检或答辩前彩排。
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY="$(cd "$DIR/.." && pwd)"
BASE="${MACP_MIN_URL:-http://127.0.0.1:8280}"
TOKEN="$(cat "${TOKEN_FILE:-$DEPLOY/secrets/write_token}")"

FAILS=()
ok()   { printf 'PASS %s%s\n' "$1" "${2:+ | $2}"; }
bad()  { printf 'FAIL %s%s\n' "$1" "${2:+ | $2}"; FAILS+=("$1"); }

jq_() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

api() { curl -sS --max-time 60 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }

tool() { # tool, task, agent, token, json-args
  local name="$1" task="$2" agent="$3" tok="$4" args="$5"
  curl -sS --max-time 300 -H "Authorization: Bearer $tok" -H "Content-Type: application/json" \
    -X POST "$BASE/v1/tools/$name/invoke" \
    -d "{\"tool_call_id\":\"$name-1\",\"agent\":\"$agent\",\"task\":\"$task\",\"attempt\":1,\"arguments\":$args}"
}

CODE="T-$(printf %08d $((RANDOM * 32768 + RANDOM)))"

echo "== 最小审计版自证 · $CODE =="

# 0. 清掉上一次演练留下的挂起派单，挑一个空闲 worker
release_stale() {
  for id in $(api "$BASE/v1/assignments?state=all&agent=$1" \
      | python3 -c "import sys,json;[print(a['id']) for a in json.load(sys.stdin) if a.get('state') in ('offered','active','claimed','submitted')]" 2>/dev/null); do
    api -X POST "$BASE/v1/assignments/$id/release" -o /dev/null
  done
}
for candidate in worker-e worker-f worker-g; do
  release_stale "$candidate"
done
AGENT=$(api "$BASE/v1/agents" \
  | python3 -c "import sys,json;print(next((a['code'] for a in json.load(sys.stdin) if a.get('role')=='worker' and a.get('status')=='idle'), ''))" 2>/dev/null)
[ -n "$AGENT" ] || { echo "FAIL 没有空闲 worker"; exit 1; }
ok "空闲 worker" "$AGENT"

# 1. 建任务（sandbox 平台，compile 门禁）
status=$(api -X POST "$BASE/v1/tasks" -o /dev/null -w '%{http_code}' -d "{
  \"code\":\"$CODE\",\"title\":\"minimal audit demo\",\"workstream\":\"stream-infra\",\"kind\":\"infra\",
  \"work_order\":{\"write_surface\":[\"src/**\"],\"acceptance\":[{\"gate\":\"compile\",\"required\":true,\"level\":\"L1\"}],
  \"execution\":{\"profile\":\"local\",\"platforms\":[\"sandbox\"]}}}")
[ "$status" = "201" ] && ok "任务创建" "$CODE" || bad "任务创建" "http=$status"

# 2. 派单与认领
status=$(api -X POST "$BASE/v1/tasks/$CODE/dispatch" -o /dev/null -w '%{http_code}' -d "{\"agent\":\"$AGENT\"}")
[ "$status" = "201" ] && ok "派单" "$AGENT" || bad "派单" "http=$status"

AID=$(api "$BASE/v1/assignments/next?agent=$AGENT" | jq_ "d.get('assignment_id','')")
TOK=$(api -X POST "$BASE/v1/assignments/$AID/claim" | jq_ "(d.get('token') or {}).get('value','')")
[ -n "$TOK" ] && ok "认领并拿到任务令牌" "len=${#TOK}" || bad "认领" "no token"

# 3. 写文件 + 跑门禁
W=$(tool workspace_write_file "$CODE" "$AGENT" "$TOK" '{"path":"src/main.py","content":"def add(a, b):\n    return a + b\n"}' | jq_ "d.get('status','')")
[ "$W" = "ok" ] && ok "受控写入" "src/main.py" || bad "受控写入" "$W"

# 先提交再跑门禁：门禁会把 .evidence/ 写进工作区，若在提交前跑，
# workspace_commit 会因为证据文件越出写面而回滚成空提交。
CMT=$(tool workspace_commit "$CODE" "$AGENT" "$TOK" '{"message":"minimal audit demo"}')
COMMITTED=$(printf '%s' "$CMT" | jq_ "(d.get('output') or {}).get('commit','')")
[ -n "$COMMITTED" ] && ok "工作区提交" "${COMMITTED:0:8}" || bad "工作区提交" "$CMT"

GATE_JSON=$(tool gate_run "$CODE" "$AGENT" "$TOK" '{"gate":"compile"}')
GATE_EXIT=$(printf '%s' "$GATE_JSON" | jq_ "(d.get('output') or {}).get('exit')")
EVID=$(printf '%s' "$GATE_JSON" | jq_ "(d.get('output') or {}).get('evidence_id','')")
[ "$GATE_EXIT" = "0" ] && ok "门禁执行" "compile exit=0" || bad "门禁执行" "exit=$GATE_EXIT"
[ -n "$EVID" ] && ok "门禁留证" "${EVID:0:8}" || bad "门禁留证" "no evidence id"

# 4. 提交候选并签发回执
SUB=$(tool workspace_submit "$CODE" "$AGENT" "$TOK" '{}')
REF=$(printf '%s' "$SUB" | jq_ "(d.get('output') or {}).get('candidate_ref','')")
COMMIT=$(printf '%s' "$SUB" | jq_ "(d.get('output') or {}).get('commit','')")
[ -n "$REF" ] && ok "候选 ref" "$REF" || bad "候选 ref" "$SUB"

REC=$(api -X POST "$BASE/v1/tasks/$CODE/receipts" -d "{
  \"candidate_ref\":\"$REF\",\"base_commit\":\"$(printf '%s' "$SUB" | jq_ "(d.get('output') or {}).get('base_commit','')")\",
  \"gates\":[{\"gate\":\"compile\",\"exit\":0,\"evidence_id\":\"$EVID\"}]}")
RID=$(printf '%s' "$REC" | jq_ "d.get('receipt_id','')")
[ -n "$RID" ] && ok "回执签发" "$RID" || bad "回执签发" "$(printf '%s' "$REC" | head -c 160)"

# 5. 验签 + 审计链
VERIFIED=$(api "$BASE/v1/receipts/$RID/verify" | jq_ "d.get('valid')")
[ "$VERIFIED" = "True" ] && ok "回执验签" "ed25519 通过" || bad "回执验签" "$VERIFIED"

CHAIN=$(api "$BASE/v1/audit/chain?limit=1000")
CHAIN_OK=$(printf '%s' "$CHAIN" | jq_ "d.get('ok')")
CHAIN_N=$(printf '%s' "$CHAIN" | jq_ "d.get('checked')")
[ "$CHAIN_OK" = "True" ] && ok "审计链完整" "$CHAIN_N 条事件" || bad "审计链" "$CHAIN"

# 回执签发后任务进入 review；合并者可能很快就把它合掉，两种都算正常
STATE=$(api "$BASE/v1/tasks/$CODE" | jq_ "d.get('state')")
case "$STATE" in
  review|merged|submitted) ok "任务状态" "$STATE" ;;
  *) bad "任务状态" "$STATE" ;;
esac

echo
if [ ${#FAILS[@]} -eq 0 ]; then
  echo "最小审计版自证通过（$CODE，commit ${COMMIT:0:8}）"
  exit 0
fi
echo "FAILURES: ${FAILS[*]}"
exit 1
