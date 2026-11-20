"""HTTP helpers for the reference agent loops."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.getenv("MACP_BASE", "http://api:8000").rstrip("/")
HEALTH_FILE = os.getenv("MACP_HEALTH_FILE", "/tmp/macp-agent-health")
TOKEN = os.getenv("MACP_WRITE_TOKEN", "").strip()
if not TOKEN and os.getenv("MACP_WRITE_TOKEN_FILE"):
    path = os.getenv("MACP_WRITE_TOKEN_FILE", "")
    if os.path.exists(path):
        TOKEN = open(path, encoding="utf-8").read().strip()
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"


def heartbeat() -> None:
    """Touch the liveness file read by the container healthcheck."""

    try:
        with open(HEALTH_FILE, "a", encoding="utf-8"):
            os.utime(HEALTH_FILE, None)
    except OSError:
        pass


_LAST_REPORT = 0.0
REPORT_INTERVAL = float(os.getenv("MACP_HEARTBEAT_INTERVAL", "30"))


def report_alive(name: str, force: bool = False) -> None:
    """Tell the server this agent process is up.

    The health file only proves the container runs; without this the console
    cannot tell a live agent from a seeded row that has no process behind it.
    Throttled, and never raises: a platform hiccup must not stop the loop.
    """

    global _LAST_REPORT
    now = time.time()
    if not force and now - _LAST_REPORT < REPORT_INTERVAL:
        return
    _LAST_REPORT = now
    try:
        req = urllib.request.Request(
            f"{BASE}/v1/agents/{name}/heartbeat", data=b"", method="POST", headers=HEADERS
        )
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception:
        pass


def request(
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
    project: str | None = None,
) -> tuple[int, object | None]:
    """Call the MACP API.

    ``token`` overrides the deployment admin token for this call.  Workers use
    the task-scoped token returned by ``claim`` so the server can attribute and
    bound every write to one agent and one task.
    """

    heartbeat()
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if project:
        headers["X-MACP-Project"] = project
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed
    except urllib.error.URLError as exc:
        return 0, str(exc)
    except TimeoutError as exc:
        return 0, str(exc)
    except Exception as exc:  # connection reset, TLS error, malformed response
        return 0, str(exc)
