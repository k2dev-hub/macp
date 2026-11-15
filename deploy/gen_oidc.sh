#!/usr/bin/env bash
# Generate the built-in OIDC signing key, client secret and admin password.
set -euo pipefail

DIR="${1:-$(dirname "$0")/secrets}"
mkdir -p "$DIR"

if [ ! -f "$DIR/oidc_signing_key.pem" ]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
    -out "$DIR/oidc_signing_key.pem" >/dev/null 2>&1
fi
if [ ! -f "$DIR/oidc_client_secret" ]; then
  openssl rand -hex 32 > "$DIR/oidc_client_secret"
fi
if [ ! -f "$DIR/oidc_admin_password" ]; then
  openssl rand -hex 16 > "$DIR/oidc_admin_password"
fi
if [ ! -f "$DIR/runner_token" ]; then
  openssl rand -hex 32 > "$DIR/runner_token"
fi
chmod 600 "$DIR/oidc_signing_key.pem" "$DIR/oidc_client_secret" "$DIR/oidc_admin_password" "$DIR/runner_token"
echo "oidc/runner secrets ready in $DIR"
