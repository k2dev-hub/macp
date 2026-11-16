#!/usr/bin/env bash
# Bring up the single-organisation Fabric network MACP anchors into.
#
#   ./up.sh              # generate crypto, start containers, create channel, deploy chaincode
#   ./up.sh --reset      # tear the network down first (deletes the ledger volume)
#   ./up.sh --down       # stop containers only
#
# Everything runs from the fabric-tools / fabric-peer images, so no Fabric
# binaries need to be installed on the host.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

CHANNEL="${FABRIC_CHANNEL:-macp}"
CC_NAME="${FABRIC_CC_NAME:-provenance}"
CC_LABEL="${FABRIC_CC_LABEL:-provenance_1.0}"
CC_VERSION="${FABRIC_CC_VERSION:-1.0}"
ORDERER=orderer.example.com:7050
PEER=peer0.org1.example.com:7051
DOMAIN=org1.example.com
TOOLS=hyperledger/fabric-tools:2.5

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

compose() { docker compose -f docker-compose.yml "$@"; }
cli()     { docker exec macp-fabric-cli "$@"; }

if [ "${1:-}" = "--down" ]; then
  compose down --remove-orphans
  say "network stopped"
  exit 0
fi

if [ "${1:-}" = "--reset" ]; then
  compose down -v --remove-orphans || true
  rm -rf crypto-config channel-artifacts
  say "network removed"
fi

for img in hyperledger/fabric-orderer:2.5 hyperledger/fabric-peer:2.5 "$TOOLS"; do
  docker image inspect "$img" >/dev/null 2>&1 || die "missing image $img (docker pull it first)"
done
# The chaincode is built by the peer in fabric-ccenv.  The network and the
# lifecycle can be prepared without it; the first invoke needs it.
docker image inspect hyperledger/fabric-ccenv:2.5 >/dev/null 2>&1 ||   say "warning: hyperledger/fabric-ccenv:2.5 is missing — chaincode build will fail until it is pulled"

# ---------------------------------------------------------------- crypto
if [ ! -d crypto-config/ordererOrganizations ]; then
  say "generating crypto material"
  docker run --rm -u "$(id -u)" -v "$HERE:/work" -w /work "$TOOLS" \
    cryptogen generate --config=/work/crypto-config.yaml --output=/work/crypto-config >/dev/null
fi

mkdir -p channel-artifacts
if [ ! -f channel-artifacts/orderer.genesis.block ]; then
  say "generating genesis block and channel transaction"
  docker run --rm -u "$(id -u)" -v "$HERE:/work" -w /work \
    -e FABRIC_CFG_PATH=/work "$TOOLS" \
    configtxgen -profile MacpOrdererGenesis -channelID system-channel \
      -outputBlock /work/channel-artifacts/orderer.genesis.block >/dev/null
  docker run --rm -u "$(id -u)" -v "$HERE:/work" -w /work \
    -e FABRIC_CFG_PATH=/work "$TOOLS" \
    configtxgen -profile MacpChannel -channelID "$CHANNEL" \
      -outputCreateChannelTx /work/channel-artifacts/${CHANNEL}.tx >/dev/null
fi

# ---------------------------------------------------------------- chaincode deps
# The peer builds the chaincode inside fabric-ccenv, which has no network.  We
# vendor the two fabric packages here (host Go, one-time download) so the build
# works offline afterwards.  `vendor/` is not committed: it is 19 MB of code we
# do not maintain.
CHAINCODE_DIR="$HERE/chaincode/provenance"
if [ ! -d "$CHAINCODE_DIR/vendor" ]; then
  command -v go >/dev/null 2>&1 || die "chaincode deps are not vendored and Go is not installed (apt-get install golang-go)"
  say "vendoring chaincode dependencies (one-time)"
  ( cd "$CHAINCODE_DIR" \
    && GO111MODULE=on GOFLAGS= GOTOOLCHAIN=local \
       GOPROXY="${GOPROXY:-https://goproxy.cn,https://proxy.golang.org,direct}" \
       go mod tidy >/dev/null 2>&1 \
    && GO111MODULE=on GOTOOLCHAIN=local \
       GOPROXY="${GOPROXY:-https://goproxy.cn,https://proxy.golang.org,direct}" \
       go mod vendor >/dev/null )
fi

# ---------------------------------------------------------------- containers
say "starting orderer, peer and cli"
compose up -d orderer.example.com peer0.org1.example.com cli

say "waiting for the orderer and peer"
for _ in $(seq 1 40); do
  cli peer channel list >/dev/null 2>&1 && break
  sleep 2
done

# ---------------------------------------------------------------- channel
if ! cli peer channel list 2>/dev/null | grep -qx "$CHANNEL"; then
  say "creating channel $CHANNEL"
  cli peer channel create -o "$ORDERER" --ordererTLSHostnameOverride orderer.example.com \
    --tls --cafile /opt/gopath/src/github.com/hyperledger/fabric/peer/crypto/ordererOrganizations/example.com/orderers/orderer.example.com/msp/tlscacerts/tlsca.example.com-cert.pem \
    -c "$CHANNEL" -f "./channel-artifacts/${CHANNEL}.tx" >/dev/null

  say "joining peer"
  cli peer channel join -b "${CHANNEL}.block" >/dev/null
else
  say "channel $CHANNEL already joined"
fi

# ---------------------------------------------------------------- chaincode
say "packaging chaincode $CC_NAME"
cli peer lifecycle chaincode package /tmp/${CC_NAME}.tar.gz \
  --path ./chaincode/${CC_NAME} --lang golang --label "$CC_LABEL" >/dev/null

if ! cli peer lifecycle chaincode queryinstalled 2>/dev/null | grep -q "$CC_LABEL"; then
  say "installing chaincode"
  cli peer lifecycle chaincode install /tmp/${CC_NAME}.tar.gz
fi

PKG_ID="$(cli peer lifecycle chaincode queryinstalled 2>/dev/null | grep "$CC_LABEL" | sed -n 's/^Package ID: \([^,]*\),.*/\1/p' | head -1)"
[ -n "$PKG_ID" ] || die "could not read the package id"

if ! cli peer lifecycle chaincode queryapproved --channelID "$CHANNEL" --name "$CC_NAME" 2>/dev/null | grep -q "sequence: 1"; then
  say "approving chaincode for Org1"
  cli peer lifecycle chaincode approveformyorg -o "$ORDERER" \
    --ordererTLSHostnameOverride orderer.example.com --tls \
    --cafile /opt/gopath/src/github.com/hyperledger/fabric/peer/crypto/ordererOrganizations/example.com/orderers/orderer.example.com/msp/tlscacerts/tlsca.example.com-cert.pem \
    --channelID "$CHANNEL" --name "$CC_NAME" --version "$CC_VERSION" \
    --package-id "$PKG_ID" --sequence 1 >/dev/null
fi

if ! cli peer lifecycle chaincode querycommitted --channelID "$CHANNEL" --name "$CC_NAME" >/dev/null 2>&1; then
  say "committing chaincode definition"
  cli peer lifecycle chaincode commit -o "$ORDERER" \
    --ordererTLSHostnameOverride orderer.example.com --tls \
    --cafile /opt/gopath/src/github.com/hyperledger/fabric/peer/crypto/ordererOrganizations/example.com/orderers/orderer.example.com/msp/tlscacerts/tlsca.example.com-cert.pem \
    --channelID "$CHANNEL" --name "$CC_NAME" --version "$CC_VERSION" --sequence 1 \
    --peerAddresses "$PEER" --tlsRootCertFiles \
    /opt/gopath/src/github.com/hyperledger/fabric/peer/crypto/peerOrganizations/${DOMAIN}/peers/peer0.${DOMAIN}/tls/ca.crt >/dev/null
fi

say "chaincode ready: $CC_NAME on channel $CHANNEL"
cli peer lifecycle chaincode querycommitted --channelID "$CHANNEL" --name "$CC_NAME"
