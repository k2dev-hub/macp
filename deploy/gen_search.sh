#!/usr/bin/env bash
set -euo pipefail
DIR="${1:-$(dirname "$0")/secrets}"
mkdir -p "$DIR"
if [ ! -f "$DIR/searxng_secret" ]; then
  openssl rand -hex 32 > "$DIR/searxng_secret"
fi
chmod 600 "$DIR/searxng_secret"
echo "search secret ready"
