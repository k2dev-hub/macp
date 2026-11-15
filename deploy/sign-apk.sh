#!/usr/bin/env bash
# Sign an APK with the release keystore, outside the build VM.
#
# Hard rule from the handover doc: the signing key never enters a build VM.  The
# APK is produced unsigned inside the android-debian VM, comes back as an
# artifact, and is signed here on macp.example.com.
#
#   ./deploy/sign-apk.sh unsigned.apk [signed.apk]
#
# First run bootstraps apksigner (the Java jar from Android build-tools) and
# creates the release keystore; both live outside git and never get printed.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/lib/config.sh"
SECRETS="${SECRETS:-$DIR/secrets}"
# Runtime tooling is state, not source: keep it under deployment.state_dir.
TOOLS="${TOOLS:-$(cfg_path deployment.state_dir)/tools}"
KS="$SECRETS/apk-release.keystore"
KS_PW_FILE="$SECRETS/apk_keystore_password"
ALIAS="${ALIAS:-macp-release}"
BUILD_TOOLS_URL="${BUILD_TOOLS_URL:-https://mirrors.cloud.tencent.com/AndroidSDK/build-tools_r34-linux.zip}"

IN="${1:?usage: sign-apk.sh <unsigned.apk> [signed.apk]}"
OUT="${2:-${IN%.apk}-signed.apk}"

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# ---- apksigner -----------------------------------------------------------
APKSIGNER="$TOOLS/apksigner/lib/apksigner.jar"
if [ ! -f "$APKSIGNER" ]; then
  say "bootstrapping apksigner into $TOOLS"
  mkdir -p "$TOOLS"
  tmp="$(mktemp -d)"
  curl -fsSL --retry 3 -o "$tmp/bt.zip" "$BUILD_TOOLS_URL"
  unzip -q "$tmp/bt.zip" -d "$tmp/x"
  src="$(find "$tmp/x" -name apksigner.jar | head -1)"
  [ -n "$src" ] || { echo "apksigner.jar not found in build-tools" >&2; exit 1; }
  mkdir -p "$TOOLS/apksigner/lib"
  cp "$src" "$APKSIGNER"
  cp "$(dirname "$src")"/../../lib/*.jar "$TOOLS/apksigner/lib/" 2>/dev/null || true
  rm -rf "$tmp"
fi

# ---- keystore ------------------------------------------------------------
mkdir -p "$SECRETS"
if [ ! -s "$KS_PW_FILE" ]; then
  umask 077
  openssl rand -base64 24 | tr -d '\n' >"$KS_PW_FILE"
fi
chmod 600 "$KS_PW_FILE"
PW="$(cat "$KS_PW_FILE")"

if [ ! -f "$KS" ]; then
  say "creating release keystore $KS (RSA 4096, 10000 days)"
  keytool -genkeypair -v \
    -keystore "$KS" -storepass "$PW" -keypass "$PW" \
    -alias "$ALIAS" -keyalg RSA -keysize 4096 -validity 10000 \
    -dname "CN=MACP Release, OU=Platform, O=MACP, L=Lab, ST=Lab, C=CN" \
    >/dev/null 2>&1
  chmod 600 "$KS"
fi

say "signing $IN"
java -jar "$APKSIGNER" sign \
  --ks "$KS" --ks-pass "pass:$PW" --key-pass "pass:$PW" --ks-key-alias "$ALIAS" \
  --out "$OUT" "$IN"

say "verifying $OUT"
java -jar "$APKSIGNER" verify --verbose --print-certs "$OUT" | sed -n '1,12p'

echo "---"
echo "unsigned: $(sha256sum "$IN" | cut -d' ' -f1)"
echo "signed:   $(sha256sum "$OUT" | cut -d' ' -f1)"
