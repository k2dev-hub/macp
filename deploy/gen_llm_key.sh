#!/usr/bin/env bash
set -euo pipefail
DIR="${1:-$(dirname "$0")/secrets}"
mkdir -p "$DIR"
if [ ! -f "$DIR/llm_master_key" ]; then
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n' > "$DIR/llm_master_key"
fi
chmod 600 "$DIR/llm_master_key"
echo "llm master key ready"
