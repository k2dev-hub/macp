#!/usr/bin/env bash
# Pre-release verification: health, audit chain, replication, and one real build
# through each of the three toolchains (Linux, Android, Windows), plus the
# browser E2E.
#
#   ./deploy/release-check.sh                 # everything
#   ./deploy/release-check.sh --skip-e2e      # builds only
#   ./deploy/release-check.sh --keep          # leave the probe tasks in place
#
# Each build probe creates a task, runs its gate in the matching VM, merges it
# and checks the artifact hash landed in the event chain — so a green run means
# the toolchain, the gate, the evidence chain and the merge path all worked.
#
# Exits non-zero on the first failed section; the summary at the end lists all.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"
. "$DIR/lib/config.sh"
. "$DIR/lib/db.sh"
# The probes talk to the deployment; resolve its URL from config so a fresh
# clone (no config) falls back to loopback instead of a specific lab address.
BASE="${E2E_BASE:-$(cfg deployment.public_url "https://localhost:9443")}"
export MACP_BASE="${MACP_BASE:-$BASE}"
export MCP_BASE="${MCP_BASE:-$BASE}"
TOKEN_FILE="${TOKEN_FILE:-$DIR/secrets/write_token}"
SKIP_E2E=0
KEEP=0
SKIP_ANDROID=0
SKIP_WINDOWS=0
SKIP_LINUX=0
SIGN_APK=""
for arg in "$@"; do
  case "$arg" in
    --skip-e2e) SKIP_E2E=1 ;;
    --skip-android) SKIP_ANDROID=1 ;;
    --skip-windows) SKIP_WINDOWS=1 ;;
    --skip-linux) SKIP_LINUX=1 ;;
    --keep) KEEP=1 ;;
    --apk=*) SIGN_APK="${arg#--apk=}" ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

RESULTS=()
FAILED=0
say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
record() { # name, status, detail
  RESULTS+=("$2|$1|${3:-}")
  [ "$2" = "PASS" ] || FAILED=1
}

section() { printf '\n=== %s ===\n' "$*"; }

# ---------------------------------------------------------------- preflight
section "preflight"
health="$(curl -sk --max-time 8 "$BASE/v1/health" || true)"
if printf '%s' "$health" | grep -q '"status":"ok"'; then
  record "health" PASS
else
  record "health" FAIL "$health"
fi

chain="$(curl -sk --max-time 15 "$BASE/v1/audit/chain?limit=5000" || true)"
if printf '%s' "$chain" | grep -q '"ok":true'; then
  record "audit chain" PASS
else
  record "audit chain" FAIL "$(printf '%s' "$chain" | head -c 200)"
fi

rep="$(psql_db -tAc 'select count(*) from pg_stat_replication' 2>/dev/null | tr -d ' ')"
if [ "${rep:-0}" -ge 1 ]; then
  record "replication" PASS "$rep client(s)"
else
  record "replication" FAIL "none"
fi

# Only MACP's own units gate the release.  A host can run unrelated services
# (this deployment shares the machine with a libvirt build node), and the E2E
# deliberately stops the scheduler, so the watchdog can fail transiently.
failed_units="$(systemctl --failed --no-legend 2>/dev/null | awk "{print \$2}" | grep -E "^macp-" | grep -vE "^macp-(e2e|watchdog)" | sort -u)"
host_units="$(systemctl --failed --no-legend 2>/dev/null | awk "{print \$2}" | grep -vE "^macp-" | sort -u | tr "\n" " ")"
failed_count="$(printf "%s" "$failed_units" | grep -c . || true)"
if [ "${failed_count:-0}" -eq 0 ]; then
  record "systemd" PASS "${host_units:+host units failing (not MACP): $host_units}"
else
  record "systemd" FAIL "$(printf "%s" "$failed_units" | tr "\n" " ")"
fi

free_gb="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
[ "${free_gb:-0}" -ge 15 ] && record "disk free" PASS "${free_gb}G" || record "disk free" FAIL "${free_gb}G"

# ------------------------------------------------------------ build probes
run_probe() { # name, script
  local name="$1" script="$2"
  section "$name"
  if python3 "$script" >"/tmp/release-${name}.log" 2>&1; then
    record "$name" PASS "$(grep -c '^PASS' "/tmp/release-${name}.log") checks"
    grep '^FAILURES' "/tmp/release-${name}.log" | tail -1
  else
    record "$name" FAIL "$(grep -E '^(FAIL|FAILURES)' "/tmp/release-${name}.log" | tail -2 | tr '\n' ' ')"
    tail -5 "/tmp/release-${name}.log"
  fi
}

[ "$SKIP_LINUX" -eq 0 ] && run_probe "linux-build" "$DIR/verify/verify-build-artifacts.py"
[ "$SKIP_ANDROID" -eq 0 ] && run_probe "android-build" "$DIR/verify/verify-android.py"
[ "$SKIP_WINDOWS" -eq 0 ] && run_probe "windows-build" "$DIR/verify/verify-windows.py"
run_probe "platform routing" "$DIR/verify/verify-platforms.py"
# 构建溯源：真实构建 → 记录 → 公共验证页 → 篡改检测
run_probe "provenance" "$DIR/verify/verify-provenance.py"

# 联盟链桥接：启用时才检查（bridge 在应用机本地，容器经网关访问）
FABRIC_URL="$(cfg fabric.bridge_url "")"
if [ -n "$FABRIC_URL" ] && [ "$(cfg fabric.enabled false)" = "true" ]; then
  if curl -sf --max-time 5 "${FABRIC_URL}/health" 2>/dev/null | grep -q '"status": *"ok"'; then
    record "fabric" PASS "$FABRIC_URL"
  else
    record "fabric" FAIL "bridge unreachable at $FABRIC_URL"
  fi
fi

# --------------------------------------------------------------------- E2E
if [ "$SKIP_E2E" -eq 0 ]; then
  section "browser e2e"
  if "$DIR/e2e/run.sh" --server >/tmp/release-e2e.log 2>&1; then
    record "browser e2e" PASS "$(grep -c '^PASS' /tmp/release-e2e.log) checks"
  else
    record "browser e2e" FAIL "$(grep -E '^FAIL' /tmp/release-e2e.log | tail -3 | tr '\n' ' ')"
  fi
fi

# ------------------------------------------------------------------ signing
if [ -n "$SIGN_APK" ]; then
  section "sign apk"
  if [ ! -f "$SIGN_APK" ]; then
    record "apk sign" FAIL "missing $SIGN_APK"
  elif out="$("$DIR/sign-apk.sh" "$SIGN_APK" "${SIGN_APK%.apk}-signed.apk" 2>&1)"; then
    record "apk sign" PASS "$(printf '%s' "$out" | grep -E '^signed:' | tail -1)"
  else
    record "apk sign" FAIL "$(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
  fi
fi

# ------------------------------------------------------------------ summary
section "summary"
for row in "${RESULTS[@]}"; do
  IFS='|' read -r status name detail <<<"$row"
  printf '%-6s %-16s %s\n' "$status" "$name" "$detail"
done

if [ "$KEEP" -eq 0 ]; then
  section "reset baseline"
  "$DIR/reset_test_data.sh" --yes 2>&1 | tail -1
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "RELEASE CHECK: PASS"
else
  echo "RELEASE CHECK: FAIL"
fi
exit "$FAILED"
