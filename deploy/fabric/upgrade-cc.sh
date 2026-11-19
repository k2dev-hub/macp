#!/usr/bin/env bash
# Upgrade the provenance chaincode on the running network.
#
#   ./upgrade-cc.sh            # bump to the next version, e.g. 1.0 -> 1.1
#   ./upgrade-cc.sh 1.2        # explicit version
#
# Why this exists: `up.sh` installs sequence 1 for a fresh network.  Changing the
# chaincode (new functions, new validation) requires a *new package id* and a
# higher sequence, otherwise the peer keeps running the old code.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

CC_NAME="${FABRIC_CC_NAME:-provenance}"
CHANNEL="${FABRIC_CHANNEL:-macp}"
ORDERER=orderer.example.com:7050
PEER=peer0.org1.example.com:7051
BASE=/opt/gopath/src/github.com/hyperledger/fabric/peer
ORDERER_CA="$BASE/crypto/ordererOrganizations/example.com/orderers/orderer.example.com/msp/tlscacerts/tlsca.example.com-cert.pem"
PEER_CA="$BASE/crypto/peerOrganizations/org1.example.com/peers/peer0.org1.example.com/tls/ca.crt"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR\033[0m %s\n' "$*" >&2; exit 1; }
cli() { docker exec macp-fabric-cli "$@"; }

docker ps --format '{{.Names}}' | grep -qx macp-fabric-cli || die "网络未启动，先运行 ./up.sh"

CURRENT="$(cli peer lifecycle chaincode querycommitted --channelID "$CHANNEL" --name "$CC_NAME" 2>/dev/null \
  | sed -n 's/.*Sequence: \([0-9]*\).*/\1/p' | head -1)"
[ -n "$CURRENT" ] || die "链码 $CC_NAME 尚未在通道 $CHANNEL 上提交"

CURRENT_VERSION="$(cli peer lifecycle chaincode querycommitted --channelID "$CHANNEL" --name "$CC_NAME" 2>/dev/null \
  | sed -n 's/.*Version: \([0-9.]*\).*/\1/p' | head -1)"

if [ -n "${1:-}" ]; then
  VERSION="$1"
else
  VERSION="$(printf '%s' "${CURRENT_VERSION:-1.0}" | awk -F. '{printf "%s.%d", $1, $2+1}')"
fi
SEQUENCE=$((CURRENT + 1))
LABEL="${CC_NAME}_${VERSION}"

say "当前 version=$CURRENT_VERSION sequence=$CURRENT → 升级到 version=$VERSION sequence=$SEQUENCE"

say "打包 $LABEL"
cli peer lifecycle chaincode package "/tmp/${CC_NAME}-${VERSION}.tar.gz" \
  --path "./chaincode/${CC_NAME}" --lang golang --label "$LABEL" >/dev/null

say "安装"
cli peer lifecycle chaincode install "/tmp/${CC_NAME}-${VERSION}.tar.gz" 2>&1 | tail -2

PKG_ID="$(cli peer lifecycle chaincode queryinstalled 2>/dev/null \
  | grep "$LABEL" | sed -n 's/^Package ID: \([^,]*\),.*/\1/p' | head -1)"
[ -n "$PKG_ID" ] || die "安装后拿不到 package id"

say "批准 Org1"
cli peer lifecycle chaincode approveformyorg -o "$ORDERER" \
  --ordererTLSHostnameOverride orderer.example.com --tls --cafile "$ORDERER_CA" \
  --channelID "$CHANNEL" --name "$CC_NAME" --version "$VERSION" \
  --package-id "$PKG_ID" --sequence "$SEQUENCE" >/dev/null

say "提交定义"
cli peer lifecycle chaincode commit -o "$ORDERER" \
  --ordererTLSHostnameOverride orderer.example.com --tls --cafile "$ORDERER_CA" \
  --channelID "$CHANNEL" --name "$CC_NAME" --version "$VERSION" --sequence "$SEQUENCE" \
  --peerAddresses "$PEER" --tlsRootCertFiles "$PEER_CA" >/dev/null

say "完成"
cli peer lifecycle chaincode querycommitted --channelID "$CHANNEL" --name "$CC_NAME"

say "冒烟：新函数是否可用"
cli peer chaincode query -C "$CHANNEL" -n "$CC_NAME" -c '{"function":"stats","Args":[]}' 2>&1 | tail -2
