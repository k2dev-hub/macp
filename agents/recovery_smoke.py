"""Transient failure recovery smoke: coordinator requeues a blocked task."""

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
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
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
        if row.get("state") in ("offered", "claimed", "active", "submitted"):
            call("POST", f"/v1/assignments/{row['id']}/release", None, ADMIN)


def main() -> int:
    if not ADMIN:
        print("FAIL no admin token")
        return 2
    reset_agent("worker-smoke")
    code = f"T-4{int(time.time()) % 10**6:06d}"
    status, _ = call(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": "transient recovery smoke",
            "workstream": "stream-core",
            "priority": "P2",
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
    status, _ = call(
        "POST",
        "/v1/agents/worker-smoke/events",
        {
            "type": "task.blocked",
            "payload": {
                "task": code,
                "reason": "HTTP 0 connection refused",
                "options": ["retry", "handoff"],
            },
        },
        token,
    )
    check("blocked with transient error", status == 202, status)
    _, detail = call("GET", f"/v1/tasks/{code}")
    check("state blocked", detail.get("state") == "blocked", detail.get("state"))

    recovered = False
    for _ in range(50):
        time.sleep(2)
        _, detail = call("GET", f"/v1/tasks/{code}")
        if detail.get("state") != "blocked":
            recovered = True
            break
        if any(
            event.get("type") == "task.requeued"
            and str((event.get("payload") or {}).get("reason", "")).startswith("auto-recovery")
            for event in detail.get("events", [])
        ):
            recovered = True
            break
    check("auto-recovery requeued task", recovered, detail.get("state"))
    print(f"result={'OK' if FAIL == 0 else 'FAIL'} pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
