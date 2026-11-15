"""Security smoke: per-agent tokens, scopes, task binding and revocation."""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.getenv("MACP_BASE", "https://localhost:9443").rstrip("/")
SSL_CONTEXT = ssl._create_unverified_context() if BASE.startswith("https://") else None
ADMIN = os.getenv("MACP_ADMIN_TOKEN", "").strip()
if not ADMIN and os.getenv("MACP_WRITE_TOKEN_FILE"):
    path = os.getenv("MACP_WRITE_TOKEN_FILE", "")
    if os.path.exists(path):
        ADMIN = open(path, encoding="utf-8").read().strip()

PASS = 0
FAIL = 0


def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, raw


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")



def reset_agent(agent: str) -> None:
    status, rows = call("GET", f"/v1/assignments?state=all&agent={agent}", None, ADMIN)
    if status != 200:
        return
    for row in rows:
        if row.get("agent") == agent and row.get("state") in (
            "offered",
            "claimed",
            "active",
            "submitted",
        ):
            call("POST", f"/v1/assignments/{row['id']}/release", None, ADMIN)


def main() -> int:
    if not ADMIN:
        print("FAIL no admin token")
        return 2
    reset_agent("worker-smoke")
    code = f"T-9{int(time.time()) % 10**6:06d}"
    status, created = call(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": "security smoke",
            "workstream": "stream-core",
            "priority": "P2",
            "budget": {"max_tool_calls": 20, "max_model_calls": 5, "max_cost": 1},
        },
        ADMIN,
    )
    check("task created", status == 201, f"{status} {created}")
    status, dispatched = call(
        "POST", f"/v1/tasks/{code}/dispatch", {"agent": "worker-smoke"}, ADMIN
    )
    check("dispatched", status == 201, f"{status} {dispatched}")
    assignment_id = (dispatched or {}).get("assignment_id")
    status, claim = call("POST", f"/v1/assignments/{assignment_id}/claim", None, ADMIN)
    token = ((claim or {}).get("token") or {}).get("value")
    check("claim issued token", status == 200 and bool(token) and token != "<demo-agent-token>")
    check(
        "claim base is a real commit",
        len(str((claim or {}).get("workspace", {}).get("base_commit", ""))) == 40,
        str((claim or {}).get("workspace")),
    )

    status, out = call(
        "POST",
        "/v1/tools/repo_list/invoke",
        {"agent": "worker-smoke", "task": code, "attempt": 1, "arguments": {"path": "."}},
        token,
    )
    check("scoped token can use tool", status == 200 and out.get("status") == "ok", f"{status} {out}")

    status, out = call(
        "POST",
        "/v1/tools/repo_list/invoke",
        {"agent": "worker-e", "task": code, "attempt": 1, "arguments": {"path": "."}},
        token,
    )
    check("token cannot act as another agent", status == 403, f"{status} {out}")

    status, out = call(
        "POST",
        "/v1/tools/repo_list/invoke",
        {"agent": "worker-smoke", "task": "T-1001", "attempt": 1, "arguments": {"path": "."}},
        token,
    )
    check("token cannot touch another task", status == 403, f"{status} {out}")

    status, out = call(
        "POST",
        "/v1/agents/worker-e/events",
        {"type": "turn.started", "payload": {"task": code}},
        token,
    )
    check("token cannot emit for another agent", status == 403, f"{status} {out}")

    status, tokens = call("GET", "/v1/admin/tokens", None, ADMIN)
    token_id = next(
        (row["id"] for row in tokens or [] if row.get("task") == code), None
    ) if status == 200 else None
    check("admin can list agent token", status == 200 and bool(token_id), f"{status} {tokens}")

    status, out = call("POST", f"/v1/admin/tokens/{token_id}/revoke", None, ADMIN)
    check("token revoked", status == 200, f"{status} {out}")

    status, out = call(
        "POST",
        "/v1/tools/repo_list/invoke",
        {"agent": "worker-smoke", "task": code, "attempt": 1, "arguments": {"path": "."}},
        token,
    )
    check("revoked token rejected", status == 401, f"{status} {out}")

    call("POST", f"/v1/assignments/{assignment_id}/release", None, ADMIN)
    print(f"result={'OK' if FAIL == 0 else 'FAIL'} pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
