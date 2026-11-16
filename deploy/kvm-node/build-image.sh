#!/usr/bin/env bash
# Build a bootable Debian trixie golden image without downloading a cloud image.
# Runs on the KVM host (build-host.example.com).  Uses the local apt mirror through
# debootstrap, installs a serial console and an SSH key for the macp user.
set -euo pipefail

NAME="${1:-debian-build}"
SIZE="${2:-6G}"
MIRROR="${MIRROR:-https://mirrors.aliyun.com/debian/}"
KEY_FILE="${KEY_FILE:-/etc/macp-kvm/node_key.pub}"
BASE_DIR="${BASE_DIR:-/var/lib/libvirt/images/macp/base}"
RAW="$BASE_DIR/$NAME.raw"
QCOW="$BASE_DIR/$NAME.qcow2"
MNT="/mnt/macp-image-$$"

mkdir -p "$BASE_DIR"
[[ -f "$KEY_FILE" ]] || { echo "missing ssh public key: $KEY_FILE" >&2; exit 1; }
if [[ -f "$QCOW" ]]; then echo "image already exists: $QCOW" >&2; exit 1; fi

cleanup() {
  set +e
  for m in dev/pts dev proc sys; do umount "$MNT/$m" 2>/dev/null; done
  umount "$MNT" 2>/dev/null
  [[ -n "${LOOP:-}" ]] && losetup -d "$LOOP" 2>/dev/null
  rmdir "$MNT" 2>/dev/null
}
trap cleanup EXIT

truncate -s "$SIZE" "$RAW"
parted -s "$RAW" mklabel gpt \
  mkpart bios-boot 1MiB 2MiB \
  set 1 bios_grub on \
  mkpart root 2MiB 100%
LOOP="$(losetup --find --show --partscan "$RAW")"
PART="${LOOP}p2"
mkfs.ext4 -q -F -L macp-root "$PART"
mkdir -p "$MNT"
mount "$PART" "$MNT"

# debootstrap only honours the LAST --include= (verified on 2026-11-14: the
# earlier three lists were dropped, leaving the image without /sbin/init,
# sshd, python3, git or curl).  Keep exactly one merged list.
PKGS="systemd,systemd-sysv,openssh-server,dbus"
PKGS="$PKGS,python3,python3-venv,git,curl,ca-certificates,rsync,tar,gzip,xz-utils"
PKGS="$PKGS,iproute2,iputils-ping,procps,sudo,qemu-guest-agent,locales,less,unzip"
PKGS="$PKGS,build-essential,dpkg-dev,fakeroot,file"
debootstrap --variant=minbase --include="$PKGS" trixie "$MNT" "$MIRROR"

# Fail loudly instead of shipping an unbootable "golden" image: debootstrap
# exits 0 even when packages from --include are unsatisfiable.
for f in sbin/init usr/sbin/sshd usr/bin/python3 usr/bin/git usr/bin/curl; do
  if [[ ! -e "$MNT/$f" ]]; then
    echo "golden image is unusable: /$f missing after debootstrap" >&2
    exit 1
  fi
done

cat > "$MNT/etc/resolv.conf" <<'EOF'
nameserver 192.168.122.1
nameserver 223.5.5.5
EOF
echo macp-builder > "$MNT/etc/hostname"
cat > "$MNT/etc/hosts" <<'EOF'
127.0.0.1 localhost
127.0.1.1 macp-builder
EOF
cat > "$MNT/etc/fstab" <<'EOF'
/dev/vda2 / ext4 defaults,noatime 0 1
EOF

mkdir -p "$MNT/etc/systemd/network"
cat > "$MNT/etc/systemd/network/20-wired.network" <<'EOF'
[Match]
Name=en*

[Network]
DHCP=yes
EOF
mkdir -p "$MNT/etc/systemd/system/multi-user.target.wants"
ln -sf /usr/lib/systemd/system/systemd-networkd.service \
  "$MNT/etc/systemd/system/multi-user.target.wants/systemd-networkd.service"
ln -sf /usr/lib/systemd/system/systemd-resolved.service \
  "$MNT/etc/systemd/system/dbus-org.freedesktop.resolve1.service" 2>/dev/null || true
mkdir -p "$MNT/etc/systemd/system/getty.target.wants"
ln -sf /lib/systemd/system/serial-getty@.service \
  "$MNT/etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service"

chroot "$MNT" useradd -m -s /bin/bash -G sudo macp
install -d -m 700 -o 1000 -g 1000 "$MNT/home/macp/.ssh"
install -m 600 -o 1000 -g 1000 "$KEY_FILE" "$MNT/home/macp/.ssh/authorized_keys"
echo 'macp ALL=(ALL) NOPASSWD:ALL' > "$MNT/etc/sudoers.d/90-macp"
chmod 440 "$MNT/etc/sudoers.d/90-macp"
install -d "$MNT/etc/ssh/sshd_config.d"
cat > "$MNT/etc/ssh/sshd_config.d/10-macp.conf" <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF

mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"
chroot "$MNT" /bin/bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update
apt-get -qq install -y linux-image-amd64 grub-pc
' >/tmp/macp-kernel.log 2>&1

cat > "$MNT/etc/default/grub" <<'EOF'
GRUB_DEFAULT=0
GRUB_TIMEOUT=1
GRUB_DISTRIBUTOR="macp"
GRUB_CMDLINE_LINUX_DEFAULT="console=ttyS0,115200n8 quiet"
GRUB_CMDLINE_LINUX=""
GRUB_TERMINAL="serial console"
GRUB_SERIAL_COMMAND="serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1"
EOF
chroot "$MNT" update-grub >/tmp/macp-grub.log 2>&1
chroot "$MNT" grub-install --target=i386-pc --recheck "$LOOP" >>/tmp/macp-grub.log 2>&1

# Drop machine-id and logs so every clone boots uniquely.
: > "$MNT/etc/machine-id"
rm -rf "$MNT/var/log/journal/"* "$MNT/var/cache/apt/archives/"*.deb
sync

cleanup
trap - EXIT
qemu-img convert -f raw -O qcow2 -c "$RAW" "$QCOW"
rm -f "$RAW"
chmod 0644 "$QCOW"
qemu-img info "$QCOW"
echo "golden image ready: $QCOW"