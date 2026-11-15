#!/usr/bin/env bash
# Generate the shared token used between the API and the signing service.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/secrets/signer_token"

umask 077
if [ -s "$OUT" ]; then
  echo "signer_token already exists"
else
  mkdir -p "$DIR/secrets"
  openssl rand -hex 32 >"$OUT"
  echo "generated $OUT"
fi
chmod 600 "$OUT"
