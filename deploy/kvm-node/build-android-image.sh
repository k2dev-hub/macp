#!/usr/bin/env bash
# Build an Android build node image for the KVM host (build-host.example.com).
#
# Same skeleton as build-image.sh (GPT + bios_grub + ext4 root, serial console,
# macp user + node_key, DHCP, qemu-guest-agent) plus an Android toolchain:
#   JDK 21, cmdline-tools, platform-tools, build-tools;34.0.0, platforms;android-35,
#   Gradle 8.9, Maven mirrors (aliyun) and a PRE-WARMED ~/.gradle cache so jobs can
#   run `./gradlew --offline` without touching the network.
#
# Usage:  ./build-android-image.sh [name] [size]
set -euo pipefail

NAME="${1:-android-build}"
SIZE="${2:-16G}"
MIRROR="${MIRROR:-https://mirrors.aliyun.com/debian/}"
KEY_FILE="${KEY_FILE:-/etc/macp-kvm/node_key.pub}"
BASE_DIR="${BASE_DIR:-/var/lib/libvirt/images/macp/base}"
RAW="$BASE_DIR/$NAME.raw"
QCOW="$BASE_DIR/$NAME.qcow2"
MNT="/mnt/macp-android-$$"

SDK_MIRROR="${SDK_MIRROR:-https://mirrors.cloud.tencent.com/AndroidSDK}"
GRADLE_VERSION="${GRADLE_VERSION:-8.9}"
ANDROID_PLATFORM="${ANDROID_PLATFORM:-android-35}"
BUILD_TOOLS="${BUILD_TOOLS:-34.0.0}"
JAVA_PKG="${JAVA_PKG:-openjdk-21-jdk-headless}"

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
mkfs.ext4 -q -F -L macp-android "$PART"
mkdir -p "$MNT"
mount "$PART" "$MNT"

# One merged --include list: debootstrap only honours the LAST one.
PKGS="systemd,systemd-sysv,openssh-server,dbus"
PKGS="$PKGS,python3,git,curl,ca-certificates,rsync,tar,gzip,xz-utils,unzip"
PKGS="$PKGS,iproute2,iputils-ping,procps,sudo,qemu-guest-agent,locales,less,file"
PKGS="$PKGS,build-essential,$JAVA_PKG"
debootstrap --variant=minbase --include="$PKGS" trixie "$MNT" "$MIRROR"

for f in sbin/init usr/sbin/sshd usr/bin/java usr/bin/unzip; do
  [[ -e "$MNT/$f" ]] || { echo "image unusable: /$f missing" >&2; exit 1; }
done

cat > "$MNT/etc/resolv.conf" <<'EOF'
nameserver 192.168.122.1
nameserver 223.5.5.5
EOF
echo macp-android > "$MNT/etc/hostname"
cat > "$MNT/etc/hosts" <<'EOF'
127.0.0.1 localhost
127.0.1.1 macp-android
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

# ---- Android toolchain ---------------------------------------------------
echo "== installing Android SDK into the image =="
mkdir -p "$MNT/opt/android-sdk/cmdline-tools" "$MNT/opt/gradle"

# cmdline-tools (the zip unpacks to cmdline-tools/, sdkmanager expects .../latest/)
curl -fsSL --retry 3 -o "$MNT/tmp/cmdline-tools.zip" \
  "$SDK_MIRROR/commandlinetools-linux-11076708_latest.zip"
if [ -s "$MNT/tmp/cmdline-tools.zip" ]; then
  unzip -q "$MNT/tmp/cmdline-tools.zip" -d "$MNT/tmp/cmdline-unpack"
  mv "$MNT/tmp/cmdline-unpack/cmdline-tools" "$MNT/opt/android-sdk/cmdline-tools/latest"
  rm -rf "$MNT/tmp/cmdline-tools.zip" "$MNT/tmp/cmdline-unpack"
else
  echo "failed to download cmdline-tools" >&2
  exit 1
fi

mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"

chroot "$MNT" /bin/bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:/usr/bin:/bin"
sdkmanager --sdk_root="$ANDROID_HOME" --licenses >/dev/null 2>&1 || true
yes | sdkmanager --sdk_root="$ANDROID_HOME" --licenses >/dev/null 2>&1 || true
sdkmanager --sdk_root="$ANDROID_HOME" "platform-tools" "build-tools;'"$BUILD_TOOLS"'" "platforms;'"$ANDROID_PLATFORM"'" 2>&1 | tail -5
sdkmanager --sdk_root="$ANDROID_HOME" --list_installed 2>/dev/null | tail -8
' || { echo "sdkmanager failed" >&2; exit 1; }

# Gradle distribution (Tencent mirror) instead of the wrapper downloading it per job
curl -fsSL --retry 3 -o "$MNT/tmp/gradle.zip" \
  "https://mirrors.cloud.tencent.com/gradle/gradle-${GRADLE_VERSION}-bin.zip"
unzip -q "$MNT/tmp/gradle.zip" -d "$MNT/opt/gradle"
rm -f "$MNT/tmp/gradle.zip"
ln -sf "/opt/gradle/gradle-${GRADLE_VERSION}/bin/gradle" "$MNT/usr/local/bin/gradle"

# Maven: aliyun mirrors, so dependency resolution never needs dl.google.com at job time
install -d -m 755 "$MNT/home/macp/.gradle/init.d"
cat > "$MNT/home/macp/.gradle/init.d/00-mirrors.gradle" <<'EOF'
// Prefer domestic mirrors; jobs are expected to run with --offline in CI.
def mirrors = [
    "https://maven.aliyun.com/repository/google",
    "https://maven.aliyun.com/repository/central",
    "https://maven.aliyun.com/repository/gradle-plugin",
    "https://maven.aliyun.com/repository/public",
]
settingsEvaluated { settings ->
    settings.pluginManagement {
        repositories {
            mirrors.each { u -> maven { url = u; allowInsecureProtocol = false } }
            gradlePluginPortal()
        }
    }
}
allprojects {
    repositories {
        mirrors.each { u -> maven { url = u } }
        google()
        mavenCentral()
    }
}
EOF
chown -R 1000:1000 "$MNT/home/macp/.gradle"

cat > "$MNT/etc/profile.d/android.sh" <<'EOF'
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$PATH"
export GRADLE_USER_HOME=/home/macp/.gradle
EOF
chmod 644 "$MNT/etc/profile.d/android.sh"

# ---- warm the Gradle cache with a tiny Android project -------------------
echo "== warming the Gradle/AGP cache (this is the slow part) =="
WARM="$MNT/home/macp/warmup"
mkdir -p "$WARM/app/src/main/java/local/macp" "$WARM/app/src/main"
cat > "$WARM/settings.gradle" <<'EOF'
pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }
dependencyResolutionManagement { repositories { google(); mavenCentral() } }
rootProject.name = "macp-warmup"
include ":app"
EOF
cat > "$WARM/build.gradle" <<'EOF'
plugins {
    id "com.android.application" version "8.7.3" apply false
}
EOF
cat > "$WARM/app/build.gradle" <<'EOF'
plugins { id "com.android.application" }
android {
    namespace "local.macp"
    compileSdk 35
    defaultConfig { applicationId "local.macp"; minSdk 24; targetSdk 35; versionCode 1; versionName "1.0" }
}
EOF
cat > "$WARM/app/src/main/AndroidManifest.xml" <<'EOF'
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
  <application android:label="macp warmup"/>
</manifest>
EOF
cat > "$WARM/app/src/main/java/local/macp/Main.java" <<'EOF'
package local.macp;
public class Main { public static void main(String[] a) {} }
EOF
cat > "$WARM/gradle.properties" <<'EOF'
org.gradle.jvmargs=-Xmx1536m
android.useAndroidX=true
EOF

chown -R 1000:1000 "$WARM"
chroot "$MNT" /bin/bash -c '
set -e
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
export GRADLE_USER_HOME=/home/macp/.gradle
export PATH="/opt/gradle/gradle-'"$GRADLE_VERSION"'/bin:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$PATH"
cd /home/macp/warmup
sudo -u macp env ANDROID_HOME=$ANDROID_HOME ANDROID_SDK_ROOT=$ANDROID_SDK_ROOT GRADLE_USER_HOME=$GRADLE_USER_HOME PATH=$PATH \
  gradle --no-daemon --console=plain :app:assembleDebug 2>&1 | tail -15
' || { echo "gradle warm-up failed" >&2; exit 1; }

# keep the warm-up project (a useful smoke fixture) but drop daemon logs
rm -rf "$MNT/home/macp/.gradle/daemon"/* || true
du -sh "$MNT/opt/android-sdk" "$MNT/home/macp/.gradle" 2>/dev/null || true

# ---- kernel / bootloader (same as the Linux image) -----------------------
mount --bind /dev "$MNT/dev" 2>/dev/null || true
chroot "$MNT" /bin/bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update
apt-get -qq install -y linux-image-amd64 grub-pc
' >/tmp/macp-android-kernel.log 2>&1 || { echo "kernel install failed"; tail -20 /tmp/macp-android-kernel.log; exit 1; }

cat > "$MNT/etc/default/grub" <<'EOF'
GRUB_DEFAULT=0
GRUB_TIMEOUT=1
GRUB_DISTRIBUTOR="macp"
GRUB_CMDLINE_LINUX_DEFAULT="console=ttyS0,115200n8 quiet"
GRUB_CMDLINE_LINUX=""
GRUB_TERMINAL="serial console"
GRUB_SERIAL_COMMAND="serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1"
EOF
chroot "$MNT" update-grub >/tmp/macp-android-grub.log 2>&1
chroot "$MNT" grub-install --target=i386-pc --recheck "$LOOP" >>/tmp/macp-android-grub.log 2>&1

: > "$MNT/etc/machine-id"
rm -rf "$MNT/var/log/journal/"* "$MNT/var/cache/apt/archives/"*.deb
sync

cleanup
trap - EXIT
qemu-img convert -f raw -O qcow2 -c "$RAW" "$QCOW"
rm -f "$RAW"
chmod 0644 "$QCOW"
qemu-img info "$QCOW" | head -5
echo "android image ready: $QCOW"
