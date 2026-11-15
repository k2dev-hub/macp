#!/usr/bin/env bash
# Copy agent PUBLIC keys into deploy/agent_pubkeys/, which is the only key
# material the API containers mount.  Private keys stay in deploy/agent_keys/
# and are mounted read-only into the signer container alone.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/agent_keys"
DST="$DIR/agent_pubkeys"

mkdir -p "$DST"
count=0
for pub in "$SRC"/*.pub; do
  [ -e "$pub" ] || continue
  cp -f "$pub" "$DST/"
  count=$((count + 1))
done
chmod 644 "$DST"/*.pub 2>/dev/null || true
echo "exported $count public keys to $DST"
ls -1 "$DST" | head -5
