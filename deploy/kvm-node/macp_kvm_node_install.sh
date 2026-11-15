#!/usr/bin/env bash
# Install macp-kvm-node as a systemd service on the KVM host (build-host.example.com).
#
#   ./macp_kvm_node_install.sh            # install + start
#   ./macp_kvm_node_install.sh --token    # (re)generate the node token
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
TOKEN_FILE=/etc/macp-kvm/node_token
PY=/usr/bin/python3

mkdir -p /etc/macp-kvm /var/log/macp-kvm-node
chmod 700 /etc/macp-kvm

if [ "${1:-}" = "--token" ] || [ ! -s "$TOKEN_FILE" ]; then
  umask 077
  openssl rand -hex 32 >"$TOKEN_FILE"
  echo "generated $TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
chmod 755 "$DIR/macp_kvm_node.py"

cat > /etc/systemd/system/macp-kvm-node.service <<EOF
[Unit]
Description=MACP KVM job node (one-shot VM runner)
After=libvirtd.service network-online.target
Requires=libvirtd.service

[Service]
Type=simple
ExecStart=${PY} ${DIR}/macp_kvm_node.py
Environment=MACP_KVM_NODE_BIND=build-host.example.com
Environment=MACP_KVM_NODE_PORT=9300
Environment=MACP_KVM_NODE_TOKEN_FILE=${TOKEN_FILE}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now macp-kvm-node.service
sleep 2
systemctl --no-pager --lines=3 status macp-kvm-node.service
curl -s --max-time 5 http://build-host.example.com:9300/health; echo
