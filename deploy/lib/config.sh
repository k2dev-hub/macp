#!/usr/bin/env bash
# Shared configuration accessor for the deploy scripts.
#
#   . "$(dirname "$0")/lib/config.sh"
#   NAS="$(cfg backup.root "")"          # "" when unset
#   KEEP="$(cfg wal.keep_hours 36)"      # default when unset
#
# Reads config/macp.toml (override with MACP_CONFIG).  The file is TOML, which
# bash cannot parse, so python3 does it; every deployment already has python3.
# Missing file or missing key returns the default, so scripts keep working on a
# standalone install.

MACP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MACP_ROOT
export MACP_CONFIG="${MACP_CONFIG:-$MACP_ROOT/config/macp.toml}"

# cfg <section.key> [default]
cfg() {
  local key="$1" default="${2-}"
  if [ ! -f "$MACP_CONFIG" ]; then
    printf '%s' "$default"
    return 0
  fi
  python3 - "$MACP_CONFIG" "$key" "$default" <<'PY'
import sys
try:
    import tomllib
except ModuleNotFoundError:
    print(sys.argv[3], end="")
    raise SystemExit(0)
path, dotted, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
except (OSError, tomllib.TOMLDecodeError):
    print(default, end="")
    raise SystemExit(0)
node = data
for part in dotted.split("."):
    if not isinstance(node, dict) or part not in node:
        print(default, end="")
        raise SystemExit(0)
    node = node[part]
if isinstance(node, bool):
    print("true" if node else "false", end="")
elif isinstance(node, list):
    print(",".join(str(item) for item in node), end="")
else:
    print(node, end="")
PY
}

# cfg_path <section.key> [default]: resolve a path relative to deployment.root.
cfg_path() {
  local value
  value="$(cfg "$1" "${2-}")"
  case "$value" in
    "" ) printf '' ;;
    /*) printf '%s' "$value" ;;
    * ) printf '%s/%s' "$(cfg deployment.root "$MACP_ROOT")" "$value" ;;
  esac
}

# cfg_enabled <section>: true when <section>.enabled is true.
cfg_enabled() {
  [ "$(cfg "$1.enabled" "false")" = "true" ]
}

# config_notice <section> <what>: print a skip notice for a disabled integration.
config_notice() {
  printf '%s: %s disabled (set [%s] in config/macp.toml to enable)\n' "$2" "$1" "$1" >&2
}
