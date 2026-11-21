#!/usr/bin/env bash
# 在 Fabric 上查一条构建锚定是否存在。
#
#   ./show-tx.sh B-20261121-D682F9
#   ./show-tx.sh B-20261121-D682F9 50      # 已知区块号时直接指定
#
# 三层证据：
#   1. 通道高度（账本长到哪了）
#   2. 链码读回（键存在，记录里带着交易号与指纹）
#   3. 区块文件（把那笔交易所在区块取下来，确认交易号真的在里面）
set -uo pipefail

BID="${1:-}"
BLOCK_ARG="${2:-}"
[ -n "$BID" ] || { echo "用法: $0 <build_id> [block]"; exit 2; }

CLI=macp-fabric-cli
CHANNEL="${FABRIC_CHANNEL:-macp}"
CC="${FABRIC_CC_NAME:-provenance}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOKEN_FILE="$ROOT/deploy/secrets/write_token"

say() { printf '\n\033[36m== %s\033[0m\n' "$*"; }
die() { printf '\033[31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

docker ps --format '{{.Names}}' | grep -qx "$CLI" || die "Fabric CLI 容器未运行：先起网络（deploy/fabric/up.sh）"

say "1. 通道高度"
docker exec "$CLI" peer channel getinfo -c "$CHANNEL" 2>/dev/null | tail -1

say "2. 链码读回：$BID"
RECORD=$(docker exec "$CLI" peer chaincode query -C "$CHANNEL" -n "$CC" \
  -c "{\"function\":\"query\",\"Args\":[\"$BID\"]}" 2>&1 | tail -1)
case "$RECORD" in
  *'"build_id"'*) : ;;
  *) die "链上没有这条记录：${RECORD:0:160}" ;;
esac

TX=$(printf '%s' "$RECORD" | sed -n 's/.*"tx_id":"\([0-9a-f]\{64\}\)".*/\1/p')
FP=$(printf '%s' "$RECORD" | sed -n 's/.*"fingerprint":"\([0-9a-f]*\)".*/\1/p')
ANCHORED=$(printf '%s' "$RECORD" | sed -n 's/.*"anchored_at":"\([^"]*\)".*/\1/p')
echo "  交易号   : ${TX:-（记录里没有）}"
echo "  指纹     : ${FP:0:32}…"
echo "  锚定时间 : ${ANCHORED:-未知}"

say "3. 交易历史（这个键被哪几笔交易动过）"
docker exec "$CLI" peer chaincode query -C "$CHANNEL" -n "$CC" \
  -c "{\"function\":\"history\",\"Args\":[\"$BID\"]}" 2>&1 | tail -1 \
  | grep -o '"tx_id":"[0-9a-f]*"\|"timestamp":"[^"]*"' | sed 's/^/  /' | head -6

[ -n "$TX" ] || die "链上记录没有交易号，无法继续"

# 区块号：参数 > 平台记录（/v1/provenance）> 扫描最近的区块
BLOCK="$BLOCK_ARG"
if [ -z "$BLOCK" ] && [ -r "$TOKEN_FILE" ]; then
  for base in "${MACP_BASE:-}" "https://127.0.0.1:9443" "https://$(hostname -I 2>/dev/null | awk '{print $1}'):9443"; do
    [ -n "$base" ] || continue
    candidate=$(curl -sk --max-time 5 -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
      "$base/v1/provenance/$BID" 2>/dev/null \
      | sed -n 's/.*"fabric_block":\([0-9]\+\).*/\1/p')
    if [ -n "$candidate" ]; then BLOCK="$candidate"; break; fi
  done
fi

if [ -z "$BLOCK" ]; then
  say "4. 区块号未知，扫描最近 30 个区块找这笔交易"
  LATEST=$(docker exec "$CLI" peer channel getinfo -c "$CHANNEL" 2>/dev/null \
    | sed -n 's/.*"height":\([0-9]*\).*/\1/p')
  [ -n "$LATEST" ] || die "取不到通道高度"
  START=$(( LATEST > 30 ? LATEST - 30 : 0 ))
  for n in $(seq "$START" $(( LATEST - 1 ))); do
    docker exec "$CLI" peer channel fetch "$n" "/tmp/scan.block" -c "$CHANNEL" >/dev/null 2>&1 || continue
    docker exec "$CLI" sh -lc 'configtxlator proto_decode --type common.Block --input /tmp/scan.block --output /tmp/scan.json' >/dev/null 2>&1 || continue
    if docker exec "$CLI" sh -lc "grep -q '$TX' /tmp/scan.json" 2>/dev/null; then
      BLOCK="$n"; break
    fi
  done
  [ -n "$BLOCK" ] || die "最近 30 个区块里没找到这笔交易（可能更早，用第二个参数指定区块号）"
  say "在区块 $BLOCK 中找到"
fi

say "4. 从区块文件确认：区块 $BLOCK"
docker exec "$CLI" peer channel fetch "$BLOCK" "/tmp/block${BLOCK}.block" -c "$CHANNEL" >/dev/null 2>&1 \
  || die "取区块 $BLOCK 失败"
docker exec "$CLI" sh -lc "configtxlator proto_decode --type common.Block \
  --input /tmp/block${BLOCK}.block --output /tmp/block${BLOCK}.json" >/dev/null 2>&1 \
  || die "解码区块失败"
HITS=$(docker exec "$CLI" sh -lc "grep -c '$TX' /tmp/block${BLOCK}.json" 2>/dev/null || echo 0)
if [ "$HITS" -ge 1 ]; then
  printf '  \033[32m✓ 交易号出现在区块 %s 中（%s 处）\033[0m\n' "$BLOCK" "$HITS"
else
  printf '  \033[31m✗ 区块 %s 里没有该交易号\033[0m\n' "$BLOCK"
  exit 1
fi

say "结论"
echo "  $BID 已在通道 $CHANNEL 的区块 $BLOCK 上确认，交易号 ${TX:0:16}…"
