#!/usr/bin/env bash
# Create the MACP Windows build node (unattended) on the KVM host build-host.example.com.
#
#   ./build-windows-vm.sh            # create + start the unattended install
#   ./build-windows-vm.sh --status   # show domain, leases, ready marker
#   ./build-windows-vm.sh --screenshot /tmp/win.ppm
#
# The Windows ISO and virtio-win are already on the NAS:
#   /mnt/nfs/system-images/SW_DVD9_WIN_ENT_LTSC_2021_64BIT_ChnSimp_MLF_X22-84402.ISO
#
# Design notes:
#  * BIOS/MBR + SATA disk: Windows Setup has native AHCI drivers, so the install
#    needs no virtio driver injection.  Slower than virtio, adequate for a build
#    node; switching to virtio later means adding virtio-win to the answer file.
#  * No "press any key to boot from CD": a headless install stalls on that
#    prompt, so we poke Enter with `virsh send-key` until the disk starts growing.
#  * OpenSSH Server is enabled by the answer file on first logon and the KVM
#    node key is authorised, so MACP drives it over SSH like the Linux nodes.
set -euo pipefail

DOMAIN="${DOMAIN:-macp-win-build}"
ISO="${ISO:-/mnt/nfs/system-images/SW_DVD9_WIN_ENT_LTSC_2021_64BIT_ChnSimp_MLF_X22-84402.ISO}"
WORK="${WORK:-/opt/macp-win}"
DISK_DIR="${DISK_DIR:-/var/lib/libvirt/images}"
DISK="$DISK_DIR/$DOMAIN.qcow2"
DISK_SIZE="${DISK_SIZE:-40G}"
MEMORY="${MEMORY:-6144}"
VCPUS="${VCPUS:-4}"
NODE_PUBKEY_FILE="${NODE_PUBKEY_FILE:-/etc/macp-kvm/node_key.pub}"
NETWORK="${NETWORK:-default}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

status() {
  say "domain:"
  virsh list --all | grep -E "Name|$DOMAIN" || true
  say "leases:"
  virsh net-dhcp-leases "$NETWORK" | grep -i "$DOMAIN" || virsh net-dhcp-leases "$NETWORK" | tail -3
  say "disk:"
  ls -lh "$DISK" 2>/dev/null || echo "  (no disk)"
}

screenshot() {
  local out="${1:-/tmp/$DOMAIN.ppm}"
  virsh screenshot "$DOMAIN" "$out" >/dev/null 2>&1 || { echo "screenshot failed"; return 1; }
  echo "screenshot: $out"
}

if [ "${1:-}" = "--status" ]; then status; exit 0; fi
if [ "${1:-}" = "--screenshot" ]; then screenshot "${2:-/tmp/$DOMAIN.ppm}"; exit 0; fi

[ -f "$ISO" ] || { echo "missing ISO: $ISO" >&2; exit 1; }
[ -f "$NODE_PUBKEY_FILE" ] || { echo "missing $NODE_PUBKEY_FILE" >&2; exit 1; }
if virsh dominfo "$DOMAIN" >/dev/null 2>&1; then
  echo "domain $DOMAIN already exists (virsh undefine $DOMAIN --remove-all-storage to rebuild)" >&2
  exit 1
fi

mkdir -p "$WORK"
chmod 700 "$WORK"

# Admin password: generated once, stored 600, never printed.
if [ ! -s "$WORK/admin_password" ]; then
  umask 077
  openssl rand -base64 18 | tr -d '\n' >"$WORK/admin_password"
fi
chmod 600 "$WORK/admin_password"
ADMIN_PW="$(cat "$WORK/admin_password")"
NODE_PUBKEY="$(cat "$NODE_PUBKEY_FILE")"
# qemu (libvirt-qemu) must be able to traverse the directory and read the ISO;
# the password file and the substituted answer file stay 600.
chmod 711 "$WORK"

# Answer file with the secrets substituted (kept 600, regenerated each run).
python3 - "$SRC_DIR/autounattend.xml" "$WORK/autounattend.xml" "$ADMIN_PW" "$NODE_PUBKEY" <<'PY'
import sys
src, dst, pw, key = sys.argv[1:5]
xml = open(src, encoding="utf-8").read()
xml = xml.replace("__ADMIN_PASSWORD__", pw).replace("NODE_PUBLIC_KEY", key)
open(dst, "w", encoding="utf-8").write(xml)
PY
chmod 600 "$WORK/autounattend.xml"

# Small ISO carrying only autounattend.xml at the root (Setup scans all media).
command -v genisoimage >/dev/null || apt-get install -y -qq genisoimage >/dev/null 2>&1
rm -f "$WORK/autounattend.iso"
mkdir -p "$WORK/iso-root"
cp "$WORK/autounattend.xml" "$WORK/iso-root/"
genisoimage -quiet -J -R -V MACPUNATTEND -o "$WORK/autounattend.iso" "$WORK/iso-root"
chmod 644 "$WORK/autounattend.iso"
say "answer ISO: $WORK/autounattend.iso ($(du -h "$WORK/autounattend.iso" | cut -f1))"

say "creating $DISK ($DISK_SIZE)"
qemu-img create -f qcow2 "$DISK" "$DISK_SIZE" >/dev/null
chown libvirt-qemu:libvirt-qemu "$DISK"

say "starting unattended install"
virt-install \
  --name "$DOMAIN" \
  --memory "$MEMORY" --vcpus "$VCPUS" \
  --disk path="$DISK",bus=sata,format=qcow2 \
  --cdrom "$ISO" \
  --disk path="$WORK/autounattend.iso",device=cdrom,bus=sata \
  --network "network=$NETWORK,model=e1000e" \
  --graphics vnc,listen=127.0.0.1 \
  --os-variant win10 \
  --boot cdrom,hd \
  --noautoconsole >/dev/null

# Headless "Press any key to boot from CD": poke Enter until the disk grows.
say "poking Enter for the boot prompt"
for _ in $(seq 1 40); do
  size="$(qemu-img info --output=json "$DISK" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["actual-size"])' 2>/dev/null || echo 0)"
  if [ "${size:-0}" -gt 2000000000 ]; then
    say "install is writing to the disk ($((size / 1024 / 1024))MB), no more keystrokes needed"
    break
  fi
  virsh send-key "$DOMAIN" KEY_ENTER >/dev/null 2>&1 || true
  sleep 3
done

say "install running. Check with: $0 --status | $0 --screenshot /tmp/win.ppm"
say "when done the guest writes C:\\macp-node-ready.txt and sshd listens on port 22"
