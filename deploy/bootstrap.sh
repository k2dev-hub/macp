#!/usr/bin/env bash
# One-command first-run setup for a fresh checkout.
#
#   ./deploy/bootstrap.sh            # create what is missing, then doctor
#   ./deploy/bootstrap.sh --force-env  # also re-render deploy/.env from config
#
# It is idempotent: existing files are never overwritten, secrets are 0600 and
# never printed, and nothing here talks to the network.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
cd "$ROOT"
. "$DIR/lib/config.sh"

SECRETS="$DIR/secrets"
FORCE_ENV=0
[ "${1:-}" = "--force-env" ] && FORCE_ENV=1

say() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }
have() { [ -s "$1" ]; }

mkdir -p "$SECRETS"
chmod 700 "$SECRETS"

# ---------------------------------------------------------------- 1. config
if [ ! -f config/macp.toml ]; then
  cp config/macp.example.toml config/macp.toml
  chmod 600 config/macp.toml
  say "created config/macp.toml from the example (edit it for your host)"
else
  say "config/macp.toml present"
fi

# ---------------------------------------------------------------- 2. secrets
gen_hex() { # file, bytes
  if ! have "$1"; then
    umask 077
    openssl rand -hex "$2" >"$1"
    chmod 600 "$1"
    say "generated $(basename "$1")"
  fi
}
gen_hex "$SECRETS/write_token" 32
gen_hex "$SECRETS/signer_token" 32
gen_hex "$SECRETS/runner_token" 32
[ "$(cfg kvm.enabled false)" = "true" ] && gen_hex "$SECRETS/kvm_node_token" 32

# The compose stack mounts these; Docker would otherwise create a *directory*
# at the mount point and the service would fail to start.
for optional in deepseek_api_key; do
  if [ ! -e "$SECRETS/$optional" ]; then
    umask 077; : >"$SECRETS/$optional"; chmod 600 "$SECRETS/$optional"
    say "created empty $optional (LLM workers stay idle until you fill it)"
  fi
done

bash "$DIR/gen_oidc.sh" "$SECRETS" >/dev/null
bash "$DIR/gen_llm_key.sh" "$SECRETS" >/dev/null
say "oidc keys / llm master key ready"

# ---------------------------------------------------------------- 3. agent keys
bash "$DIR/gen_agent_keys.sh" "$DIR/agent_keys" >/dev/null
bash "$DIR/export_pubkeys.sh" >/dev/null
say "agent signing keys ready ($(ls -1 "$DIR/agent_keys"/*.pem 2>/dev/null | wc -l) keys)"

# ---------------------------------------------------------------- 4. TLS
if [ ! -f "$DIR/tls/macp.crt" ]; then
  bash "$DIR/gen_tls.sh" "$DIR/tls" >/dev/null 2>&1 || \
    say "warning: TLS generation failed (install openssl) — HTTPS will not start"
  [ -f "$DIR/tls/macp.crt" ] && say "generated self-signed TLS for $(cfg deployment.public_url)"
fi

# ---------------------------------------------------------------- 5. searxng
if [ ! -f "$DIR/searxng/settings.yml" ]; then
  umask 077
  secret="$(openssl rand -hex 32)"
  sed "s/__SEARXNG_SECRET__/$secret/" "$DIR/searxng/settings.example.yml" \
    >"$DIR/searxng/settings.yml"
  chmod 600 "$DIR/searxng/settings.yml"
  say "generated deploy/searxng/settings.yml (search instance key)"
fi

# ---------------------------------------------------------------- 6. env
if [ "$FORCE_ENV" = "1" ] || [ ! -f "$DIR/.env" ]; then
  python3 "$DIR/render_env.py" >/dev/null
  chmod 600 "$DIR/.env"
  say "rendered deploy/.env from config/macp.toml"
fi

# ---------------------------------------------------------------- 7. doctor
echo
python3 -m server.settings doctor || true

cat <<'NEXT'

Next steps
  1. Edit config/macp.toml if you want a different public_url / publish address,
     then: python3 deploy/render_env.py
  2. docker compose -f deploy/docker-compose.yml up -d --build
  3. Read the admin token (never commit it):
       cat deploy/secrets/write_token
  4. Open https://<public_url>/ and sign in with the admin token, or use SSO
     with the password in deploy/secrets/oidc_admin_password.
NEXT
