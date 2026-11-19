#!/usr/bin/env bash
# 上链演示（课设演示用）：一条命令走完"构建 → 上链 → 核对 → 篡改 → 巡检"。
#
#   ./demo.sh              # 每幕之间暂停，等你按回车
#   ./demo.sh --auto       # 连续跑完，不暂停（录屏/彩排用）
#   ./demo.sh --skip-build # 跳过第 2 幕（不重新构建，只演示账本部分）
#
# 前提：Fabric 网络与 macp-fabric-bridge 已启动，MACP 主栈健康。
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

AUTO=0
SKIP_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --auto) AUTO=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

BOLD=$'\033[1m'; CYAN=$'\033[36m'; GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'

act()   { printf '\n%s%s──── %s ────%s\n' "$BOLD" "$CYAN" "$*" "$OFF"; }
say()   { printf '  %s\n' "$*"; }
dim()   { printf '  %s%s%s\n' "$DIM" "$*" "$OFF"; }
ok()    { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
bad()   { printf '  %s✗%s %s\n' "$RED" "$OFF" "$*"; }
pause() { [ "$AUTO" -eq 1 ] && return 0; printf '\n%s（回车继续）%s' "$DIM" "$OFF"; read -r _; }

TOKEN="$(cat deploy/secrets/write_token 2>/dev/null)"
[ -n "$TOKEN" ] || { echo "找不到 deploy/secrets/write_token，先在仓库根运行 ./deploy/bootstrap.sh"; exit 1; }
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PUBLIC="${MACP_PUBLIC_URL:-https://${HOST_IP:-localhost}:9443}"
FABRIC_URL="${MACP_FABRIC_URL:-http://172.28.0.1:9310}"
FABRIC_TOKEN="$(cat deploy/secrets/fabric_bridge_token 2>/dev/null)"

# ---------------------------------------------------------------- 0. 开场
clear 2>/dev/null
printf '%sMACP 上链演示%s\n' "$BOLD" "$OFF"
say "系统：多智能体协作平台，每次构建都会生成一条可验证的溯源记录"
say "本演示：记录指纹如何离开数据库、写进联盟链，以及如何证明它没被改过"
dim "议题顺序：现状 → 构建 → 上链 → 核对 → 篡改 → 巡检"
pause

# ---------------------------------------------------------------- 1. 现状
act "第 1 幕 · 现状：链上有多少东西"

if ! curl -sf --max-time 5 "$FABRIC_URL/health" >/dev/null 2>&1; then
  bad "链桥不可达（$FABRIC_URL）"
  say "先启动：systemctl start macp-fabric-bridge 与 deploy/fabric/up.sh"
  exit 1
fi
ok "链桥在线（$FABRIC_URL）"

STATS="$(curl -s --max-time 15 -H "Authorization: Bearer $FABRIC_BRIDGE_TOKEN" "$FABRIC_URL/stats" 2>/dev/null)"
STATS="$STATS" python3 - <<'PY'
import json, os
raw = os.environ.get("STATS") or "{}"
try:
    data = json.loads(raw)
except ValueError:
    data = {}
print(f"  链上锚定总数 : {data.get('total', '-')}")
print(f"  最早 / 最新  : {data.get('first_anchored', '-')} / {data.get('last_anchored', '-')}")
PY
dim "链上只有指纹与最小元数据；源码与产物留在链下，这是设计取舍，不是缺失。"
pause

# ---------------------------------------------------------------- 2. 构建
if [ "$SKIP_BUILD" -eq 0 ]; then
  act "第 2 幕 · 一次真实构建，记录与上链同时发生"
  say "下面会：建任务 → 派单 → 认领 → 写文件 → 提交 → 门禁（Linux VM 真实构建）→ 提交候选 → 回执"
  say "然后自动做一次篡改实验：改产物 → 验证失败 → 还原 → 再次通过"
  echo
  JSON_OUT="$(mktemp)"
  MACP_BASE="$PUBLIC" MACP_EXPECT_FABRIC=1 \
    python3 deploy/verify/verify-provenance.py --json-out "$JSON_OUT" 2>&1 | sed 's/^/  /'
  BUILD_ID="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['build_id'])" "$JSON_OUT" 2>/dev/null)"
  rm -f "$JSON_OUT"
  [ -n "$BUILD_ID" ] && ok "本次构建编号：$BUILD_ID"
  pause
else
  act "第 2 幕 · 跳过构建，直接看已有记录"
fi

# ---------------------------------------------------------------- 3. 上链
act "第 3 幕 · 上链结果：数据库里的交易号与链上读回"

LEDGER="$(curl -sk --max-time 30 -H "Authorization: Bearer $TOKEN" "$PUBLIC/v1/fabric/ledger?limit=5" 2>/dev/null)"
LEDGER="$LEDGER" python3 - <<'PY'
import json, os
try:
    view = json.loads(os.environ.get("LEDGER") or "{}")
except ValueError:
    view = {}
chain = view.get("chain") or {}
totals = view.get("totals") or {}
if not chain.get("enabled"):
    print("  联盟链未启用")
    raise SystemExit
print(f"  通道 {chain.get('channel')} · 链码 {chain.get('chaincode')} · 区块高度 {chain.get('block_height')}")
print(f"  本地记录 {totals.get('local_records')} · 已锚定 {totals.get('anchored')} · 链上 {totals.get('on_chain')}")
print()
print(f"  {'状态':<10}{'构建编号':<18}{'链上指纹':<20}{'锚定时间':<21}")
print("  " + "-" * 70)
for item in (view.get("comparison") or []):
    print(f"  {item['state']:<10}{item['build_id']:<18}"
          f"{(item['chain_fingerprint'] or '')[:16]:<20}{(item['anchored_at'] or '')[:19]:<21}")
PY
dim "consistent = 链上与本地指纹一致；mismatch 在正常系统里应当永远为 0。"
pause

# ---------------------------------------------------------------- 4. 核对
act "第 4 幕 · 全量核对：链上每一条都对回数据库"

python3 deploy/fabric/verify-ledger.py --limit 200 2>&1 | sed 's/^/  /'
pause

# ---------------------------------------------------------------- 5. 收尾
act "第 5 幕 · 可以给评委看的两个页面"

LATEST="${BUILD_ID:-}"
[ -z "$LATEST" ] && LATEST="$(printf '%s' "$LEDGER" | python3 -c "import sys,json;d=json.load(sys.stdin);c=(d.get('comparison') or [{}]);print(c[0].get('build_id','') or '')" 2>/dev/null)"
if [ -n "$LATEST" ]; then
  say "单条验证页： $PUBLIC/verify/$LATEST"
  say "  扫码打开，看到产物指纹、构建人、联盟链交易号，以及三项校验结果"
fi
say "账本总览页： $PUBLIC/chain"
say "  链上最近 50 条锚定，与本地记录逐条对照"
echo
dim "演示结束。可以现场打开一个验证页，把地址改成不存在的编号，展示 404 行为。"
