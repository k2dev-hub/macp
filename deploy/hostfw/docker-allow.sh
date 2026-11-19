#!/bin/sh
# Let docker bridge traffic through this host's nftables `hostfw` table.
#
# Why this exists
#   The compute host ships a default-drop nftables ruleset that only permits the
#   LAN, so every container network needs an explicit exception.  Two networks
#   matter here:
#     172.28.0.0/16   MACP application stack
#     172.19.0.0/16   Fabric network (orderer, peer, cli)
#   Without the second one the peer cannot reach the orderer and every chaincode
#   call dies with "context deadline exceeded" while the containers look healthy.
#
# Why it runs after docker.service
#   dockerd rewrites net.bridge.bridge-nf-call-iptables to 1 on every start, which
#   sends bridged frames through the host's filter tables again.  Re-applying the
#   sysctls here keeps container-to-container traffic off that path.
#
# Usage
#   install -m 0755 deploy/hostfw/docker-allow.sh /usr/local/sbin/macp-docker-fw.sh
#   systemctl daemon-reload && systemctl restart macp-docker-fw
set -e

NETWORKS="${MACP_DOCKER_NETWORKS:-172.28.0.0/16 172.19.0.0/16}"

nft list table inet hostfw >/dev/null 2>&1 || exit 0

for net in $NETWORKS; do
  nft list table inet hostfw | grep -q "saddr $net accept" || \
    nft add rule inet hostfw input ip saddr "$net" accept
  nft list table inet hostfw | grep -q "daddr $net accept" || {
    nft add rule inet hostfw forward ip saddr "$net" accept
    nft add rule inet hostfw forward ip daddr "$net" accept
  }
done

sysctl -qw net.bridge.bridge-nf-call-iptables=0
sysctl -qw net.bridge.bridge-nf-call-ip6tables=0
