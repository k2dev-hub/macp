"""Cost budget smoke: server-side pricing and authoritative cost enforcement."""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("MACP_BASE", "https://localhost:9443").rstrip("/")
SSL_CONTEXT = ssl._create_unverified_context() if BASE.startswith("https://") else None
ADMIN = os.getenv("MACP_ADMIN_TOKEN", "").strip()
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
    code = f"T-6{int(time.time()) % 10**6:06d}"
    status, _ = call(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": "cost budget smoke",
            "workstream": "stream-core",
            "priority": "P2",
            "budget": {"max_tool_calls": 5, "max_model_calls": 5, "max_cost": 0.000001},
        },
        ADMIN,
    )
    check("task created", status == 201, status)
    status, dispatched = call(
        "POST", f"/v1/tasks/{code}/dispatch", {"agent": "worker-smoke"}, ADMIN
    )
    check("dispatched", status == 201, f"{status} {dispatched}")
    status, claim = call(
        "POST", f"/v1/assignments/{dispatched['assignment_id']}/claim", None, ADMIN
    )
    token = claim["token"]["value"]
    check("claimed", status == 200, status)
    status, out = call(
        "POST",
        f"/v1/agents/worker-smoke/events",
        {
            "type": "model.usage",
            "payload": {
                "task": code,
                "model": "deepseek-flash",
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_cache_hit_tokens": 0,
            },
        },
        token,
    )
    check("usage accepted", status == 202, f"{status} {out}")
    _, detail = call("GET", f"/v1/tasks/{code}")
    check("task blocked on cost", detail.get("state") == "blocked", detail.get("state"))
    call("POST", f"/v1/assignments/{dispatched['assignment_id']}/release", None, ADMIN)
    _, tasks = call("GET", "/v1/tasks")
    print(f"result={'OK' if FAIL == 0 else 'FAIL'} pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
