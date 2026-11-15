"""Deployment settings: one TOML file instead of hard-coded hosts and paths.

The platform used to know about one specific lab (macp.example.com, a NAS mount, a
KVM host).  Everything deployment-specific now lives in ``config/macp.toml``
(gitignored) whose committed template is ``config/macp.example.toml``.

Rules:

* a missing file is fine -- the built-in defaults describe a standalone install;
* every integration is optional: ``enabled = false`` or an empty address means
  the feature is skipped, not that the process crashes;
* environment variables win over the file (``MACP_<SECTION>_<KEY>``), which is how
  the containers receive their settings;
* nothing in here ever prints or returns a secret: only file *paths* are stored.

    python3 -m server.settings            # show the effective configuration
    python3 -m server.settings doctor     # validate + probe enabled integrations
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on very old interpreters
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "macp.toml"
EXAMPLE_CONFIG = ROOT / "config" / "macp.example.toml"

# Built-in defaults: a single-host install that talks to nothing external.
DEFAULTS: dict[str, dict[str, Any]] = {
    "deployment": {
        "public_url": "http://localhost:9443",
        "root": "",
        "state_dir": "/var/lib/macp",
        "timezone": "UTC",
    },
    "api": {
        "internal_base": "http://api:8000",
        "publish_address": "127.0.0.1",
        "publish_port": 9443,
        "proxy_read_timeout": 3600,
    },
    "database": {"host": "db", "port": 5432, "name": "macp", "user": "macp",
                 "standby_hosts": []},
    "gitea": {"enabled": False, "url": "", "repository": "", "ssh_port": 22,
              "branch": "main", "token_file": "secrets/gitea_token"},
    "backup": {"enabled": False, "root": "", "archive_dir": "_archive/macp-backups",
               "wal_mirror_dir": "_archive/macp-wal", "keep_local": 3,
               "retention_days": 30},
    "wal": {"archive_dir": "/var/lib/postgresql/wal-archive",
            "host_archive_dir": "/opt/macp/wal-archive",
            "archive_timeout_seconds": 300, "keep_hours": 36, "keep_recent": 64,
            "min_size_bytes": 10 * 1024 ** 3},
    "audit": {"anchor_dir": "", "events_archive_dir": ""},
    "kvm": {"enabled": False, "node_url": "http://127.0.0.1:9300", "node_host": "",
            "node_user": "root", "token_file": "secrets/kvm_node_token",
            "image_dir": "/var/lib/libvirt/images/macp/base",
            "windows_domain": "macp-win-build", "windows_workdir": r"C:\macp\work"},
    "signer": {"enabled": True, "url": "http://signer:9300",
               "token_file": "secrets/signer_token", "private_key_dir": "deploy/agent_keys",
               "public_key_dir": "deploy/agent_pubkeys"},
    "oidc": {"enabled": True, "issuer": "", "client_id": "macp",
             "client_secret_file": "secrets/oidc_client_secret",
             "admin_password_file": "secrets/oidc_admin_password",
             "redirect_uris": ["http://localhost:5443/", "http://localhost:5173/"]},
    "runner": {"enabled": True, "url": "http://runner:9200",
               "token_file": "secrets/runner_token", "workspace_root": "/app/workspace",
               "workspace_bytes": 512 * 1024 ** 2},
    "mcp": {"enabled": True, "project": "demo", "session_ttl_seconds": 14400,
            "max_sessions": 200, "rate_limit_per_minute": 60, "max_output_chars": 20000},
    "observability": {"metrics": True, "watchdog_enabled": True,
                      "disk_free_min_bytes": 15 * 1024 ** 3,
                      "wal_archive_max_bytes": 60 * 1024 ** 3},
    "e2e": {"enabled": True, "browser": "", "chromium_path": "/usr/bin/chromium",
            "output_dir": "deploy/e2e/out"},
}


def _coerce(value: str, like: Any) -> Any:
    """Turn an environment string into the type of the default value."""

    if isinstance(like, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int) and not isinstance(like, bool):
        try:
            return int(value)
        except ValueError:
            return like
    if isinstance(like, list):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings:
    """Dot-path access over the merged configuration."""

    def __init__(self, data: dict[str, dict[str, Any]], source: Path | None):
        self._data = data
        self.source = source

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self._data.get(name)
        return dict(value) if isinstance(value, dict) else {}

    # -- derived helpers -------------------------------------------------
    def path(self, relative: str | None) -> Path | None:
        """Resolve a configured path: absolute stays absolute, relative is under root."""

        if not relative:
            return None
        candidate = Path(relative)
        if candidate.is_absolute():
            return candidate
        return Path(str(self.get("deployment.root") or ROOT)) / candidate

    @property
    def public_url(self) -> str:
        return str(self.get("deployment.public_url") or "").rstrip("/")

    @property
    def api_internal(self) -> str:
        return str(self.get("api.internal_base") or "").rstrip("/")

    @property
    def oidc_issuer(self) -> str:
        configured = str(self.get("oidc.issuer") or "").strip()
        if configured:
            return configured.rstrip("/")
        return f"{self.public_url}/oidc"

    @property
    def nas_root(self) -> Path | None:
        if not self.get("backup.enabled"):
            return None
        root = str(self.get("backup.root") or "").strip()
        return Path(root) if root else None

    @property
    def nas_backup_dir(self) -> Path | None:
        root = self.nas_root
        return root / str(self.get("backup.archive_dir")) if root else None

    @property
    def nas_wal_dir(self) -> Path | None:
        root = self.nas_root
        return root / str(self.get("backup.wal_mirror_dir")) if root else None

    def enabled(self, section: str) -> bool:
        return bool(self.get(f"{section}.enabled", False))

    def describe(self) -> dict[str, Any]:
        """Effective configuration with secrets reduced to file paths."""

        out: dict[str, Any] = {}
        for name, values in self._data.items():
            out[name] = {
                key: (str(value) if isinstance(value, Path) else value)
                for key, value in (values or {}).items()
            }
        return out


def load(path: str | Path | None = None) -> Settings:
    """Read the TOML file, apply MACP_* overrides, fall back to defaults."""

    candidate = Path(path or os.getenv("MACP_CONFIG") or DEFAULT_CONFIG)
    data: dict[str, dict[str, Any]] = {
        section: dict(values) for section, values in DEFAULTS.items()
    }
    source: Path | None = None
    if candidate.is_file() and tomllib is not None:
        with candidate.open("rb") as handle:
            loaded = tomllib.load(handle)
        for section, values in loaded.items():
            if isinstance(values, dict):
                data.setdefault(section, {}).update(values)
            else:
                data[section] = values  # type: ignore[assignment]
        source = candidate

    for env_name, raw in os.environ.items():
        if not env_name.startswith("MACP_") or env_name == "MACP_CONFIG":
            continue
        parts = env_name[len("MACP_"):].lower().split("__")
        if len(parts) != 2:
            continue
        section, key = parts
        if section in DEFAULTS and key in DEFAULTS[section]:
            data[section][key] = _coerce(raw, DEFAULTS[section][key])
    return Settings(data, source)


SETTINGS = load()


# --------------------------------------------------------------------- doctor
def _docker_state(container: str) -> str | None:
    """Health/state of a compose container, or None when docker is unavailable."""

    import subprocess

    try:
        result = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
             container],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return "missing"
    return (result.stdout or "").strip() or "unknown"


def _probe(url: str, timeout: float = 3.0) -> str:
    """HTTP probe; container-name hosts are checked through docker instead."""

    host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host and "." not in host and host != "localhost":
        state = _docker_state(f"macp-{host}")
        if state is not None:
            return f"container {state}" if state == "missing" else f"container {state}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return f"reachable (HTTP {response.status})"
    except urllib.error.HTTPError as exc:
        return f"reachable (HTTP {exc.code})"
    except (urllib.error.URLError, OSError) as exc:
        return f"unreachable: {exc}"


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def doctor(settings: Settings) -> tuple[int, list[tuple[str, str, str]]]:
    """Check the configuration; only enabled integrations are contacted."""

    rows: list[tuple[str, str, str]] = []

    def add(name: str, state: str, detail: str = "") -> None:
        rows.append((name, state, detail))

    if settings.source:
        add("config", "ok", str(settings.source))
    else:
        add("config", "defaults", f"no file at {DEFAULT_CONFIG} (running standalone)")

    add("public_url", "ok", settings.public_url or "(unset)")

    db = settings.section("database")
    host, port = str(db.get("host")), int(db.get("port") or 5432)
    if "." not in host and host != "localhost":
        state = _docker_state(f"macp-{host}")
        add("database", "ok" if state in ("running", "healthy") else (state or "down"),
            f"container macp-{host}")
    else:
        add("database", "ok" if _port_open(host, port) else "down", f"{host}:{port}")

    for section, url_key, probe in (
        ("signer", "signer.url", "/health"),
        ("runner", "runner.url", "/health"),
        ("mcp", "mcp.enabled", None),
    ):
        if section == "mcp":
            continue
        if not settings.enabled(section):
            add(section, "disabled")
            continue
        base = str(settings.get(url_key) or "").rstrip("/")
        add(section, "configured", f"{base} -> {_probe(base + probe) if base else 'no url'}")

    kvm = settings.section("kvm")
    if settings.enabled("kvm"):
        base = str(kvm.get("node_url") or "").rstrip("/")
        add("kvm", "configured", f"{base} -> {_probe(base + '/health') if base else 'no url'}")
    else:
        add("kvm", "disabled", "only sandbox/local execution profiles")

    backup = settings.section("backup")
    if settings.enabled("backup") and settings.nas_root:
        root = settings.nas_root
        state = "ok" if root.is_dir() and os.access(root, os.W_OK) else "unwritable"
        add("backup", state, str(root))
    else:
        add("backup", "disabled", "local backups only")

    gitea = settings.section("gitea")
    if settings.enabled("gitea") and gitea.get("url"):
        add("gitea", "configured", f"{gitea['url']} -> {_probe(str(gitea['url']))}")
    else:
        add("gitea", "disabled", "source is managed manually / by the local repo")

    for name, path in (
        ("signer keys", settings.path(str(settings.get("signer.private_key_dir") or ""))),
        ("public keys", settings.path(str(settings.get("signer.public_key_dir") or ""))),
    ):
        add(name, "ok" if path and path.exists() else "missing", str(path or "(unset)"))

    failures = [row for row in rows if row[1] in ("down", "unwritable", "missing")]
    return (1 if failures else 0), rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server.settings")
    parser.add_argument("command", nargs="?", default="show", choices=["show", "doctor", "json"])
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    settings = load(args.config)
    if args.command == "json":
        print(json.dumps(settings.describe(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "doctor":
        code, rows = doctor(settings)
        width = max(len(row[0]) for row in rows) + 2
        for name, state, detail in rows:
            print(f"{name:<{width}} {state:<12} {detail}")
        print()
        print("doctor:", "FAIL" if code else "OK")
        return code

    for section, values in settings.describe().items():
        print(f"[{section}]")
        for key, value in values.items():
            print(f"  {key} = {value!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
