#!/usr/bin/env python3
"""Render deploy/.env from config/macp.toml.

Compose and the shell scripts read a handful of variables from ``deploy/.env``;
the single source of truth is the TOML file.  This script merges the values it
owns into the existing ``.env`` (everything else, e.g. secrets paths, is left
untouched) and backs the file up first.

    python3 deploy/render_env.py            # write deploy/.env
    python3 deploy/render_env.py --dry-run  # print what would change
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.settings import load  # noqa: E402

ENV_FILE = ROOT / "deploy" / ".env"
MANAGED = [
    ("MACP_PUBLIC_URL", lambda s: s.public_url),
    ("MACP_PUBLISH_ADDRESS", lambda s: s.get("api.publish_address")),
    ("MACP_PUBLISH_PORT", lambda s: s.get("api.publish_port")),
    ("MACP_HTTP_PORT", lambda s: 8088),
    ("MACP_DB_HOST", lambda s: s.get("database.host")),
    ("MACP_DB_PORT", lambda s: s.get("database.port")),
    ("MACP_DB_NAME", lambda s: s.get("database.name")),
    ("MACP_DB_USER", lambda s: s.get("database.user")),
    ("MACP_WAL_ARCHIVE_TIMEOUT", lambda s: s.get("wal.archive_timeout_seconds")),
    ("MACP_KVM_NODE_URL", lambda s: s.get("kvm.node_url") if s.enabled("kvm") else ""),
    ("MACP_OIDC_ISSUER", lambda s: s.oidc_issuer),
    ("MACP_OIDC_REDIRECT_URIS", lambda s: ",".join(s.get("oidc.redirect_uris") or [])),
    ("MACP_MCP_PROJECT", lambda s: s.get("mcp.project")),
    ("MACP_MCP_ENABLED", lambda s: "1" if s.enabled("mcp") else "0"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    settings = load(args.config)
    desired = {}
    for key, getter in MANAGED:
        value = getter(settings)
        if value is None:
            continue
        desired[key] = str(value)

    existing: dict[str, str] = {}
    order: list[str] = []
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0]
            existing[key] = line
            order.append(key)

    lines: list[str] = []
    for key in order:
        if key in desired:
            lines.append(f"{key}={desired[key]}")
        else:
            lines.append(existing[key])
    for key, value in desired.items():
        if key not in existing:
            lines.append(f"{key}={value}")

    text = "# Generated from config/macp.toml by deploy/render_env.py — do not edit.\n"
    text += "\n".join(lines) + "\n"

    if args.dry_run:
        print(text)
        return 0
    if ENV_FILE.is_file():
        backup = ENV_FILE.with_suffix(".bak")
        backup.write_text(ENV_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    ENV_FILE.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {ENV_FILE} ({len(lines)} keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
