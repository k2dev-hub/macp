#!/usr/bin/env bash
# Generate per-agent Ed25519 receipt keys and SSH commit-signing keys.
set -euo pipefail
DIR="${1:-$(dirname "$0")/agent_keys}"
mkdir -p "$DIR"
AGENTS="coordinator-1 observer-1 worker-a worker-b worker-c worker-d worker-e worker-f worker-g worker-smoke worker-llm worker-deepseek"
: > "$DIR/allowed_signers"
for agent in $AGENTS; do
  if [ ! -f "$DIR/$agent.pem" ]; then
    openssl genpkey -algorithm ED25519 -out "$DIR/$agent.pem" >/dev/null 2>&1
    openssl pkey -in "$DIR/$agent.pem" -pubout -out "$DIR/$agent.pub" >/dev/null 2>&1
  fi
  if [ ! -f "$DIR/${agent}_ssh" ]; then
    ssh-keygen -t ed25519 -N "" -C "${agent}@macp.local" -f "$DIR/${agent}_ssh" -q
  fi
  echo "${agent}@macp.local namespaces=\"git\" $(cat "$DIR/${agent}_ssh.pub")" >> "$DIR/allowed_signers"
  chmod 600 "$DIR/$agent.pem" "$DIR/${agent}_ssh"
done
echo "agent keys ready in $DIR"
