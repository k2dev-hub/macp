#!/usr/bin/env bash
# Generate an internal CA and a server certificate for the MACP LAN endpoint.
# For a real deployment, replace these files with a certificate from your PKI.
set -euo pipefail

DIR="${1:-$(dirname "$0")/tls}"
. "$(dirname "$0")/lib/config.sh"
# SAN comes from deployment.public_url (override with MACP_TLS_IP / MACP_TLS_HOST).
PUBLIC="$(cfg deployment.public_url "https://localhost:9443")"
PUBLIC_HOST="${PUBLIC#*://}"; PUBLIC_HOST="${PUBLIC_HOST%%:*}"
PUBLIC_HOST="${PUBLIC_HOST%%/*}"
IP="${MACP_TLS_IP:-}"
HOST="${MACP_TLS_HOST:-}"
if [ -z "$IP" ] && [ -z "$HOST" ]; then
  case "$PUBLIC_HOST" in
    *[!0-9.]*) HOST="$PUBLIC_HOST" ;;   # not an IPv4 literal -> DNS name
    *) IP="$PUBLIC_HOST" ;;
  esac
fi
mkdir -p "$DIR"
cd "$DIR"

if [ -f macp.crt ] && [ -f macp.key ]; then
  echo "tls already present in $DIR"
  exit 0
fi

openssl req -x509 -newkey rsa:4096 -sha256 -days 1825 -nodes \
  -keyout ca.key -out ca.crt -subj "/CN=MACP Internal CA" >/dev/null 2>&1

openssl req -newkey rsa:2048 -nodes \
  -keyout macp.key -out macp.csr -subj "/CN=macp" >/dev/null 2>&1

cat > san.cnf <<EOF
subjectAltName=IP:${IP:-127.0.0.1},IP:127.0.0.1,DNS:${HOST:-localhost},DNS:localhost
extendedKeyUsage=serverAuth
keyUsage=digitalSignature,keyEncipherment
EOF

openssl x509 -req -in macp.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out macp.crt -days 825 -sha256 -extfile san.cnf >/dev/null 2>&1

rm -f macp.csr san.cnf
chmod 600 macp.key ca.key
echo "generated $DIR/macp.crt for ${IP}"
