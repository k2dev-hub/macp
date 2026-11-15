"""L2 smoke: tests-only change requires unit+integration and merges without approval."""

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
    code = f"T-5{int(time.time()) % 10**6:06d}"
    status, created = call(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": "L2 tests change",
            "workstream": "stream-core",
            "priority": "P2",
            "budget": {"max_tool_calls": 30, "max_model_calls": 8, "max_cost": 2},
            "work_order": {
                "write_surface": ["solution.py", "tests/**"],
                "acceptance": [{"gate": "compile", "required": True, "level": "L1"}],
            },
        },
        ADMIN,
    )
    check("L2 task created", status == 201, f"{status} {created}")
    status, dispatched = call(
        "POST", f"/v1/tasks/{code}/dispatch", {"agent": "worker-smoke"}, ADMIN
    )
    check("dispatched", status == 201, f"{status} {dispatched}")
    assignment = (dispatched or {}).get("assignment_id")
    status, claim = call("POST", f"/v1/assignments/{assignment}/claim", None, ADMIN)
    token = ((claim or {}).get("token") or {}).get("value")
    attempt = (claim or {}).get("attempt", 1)
    check("claimed", status == 200 and bool(token), f"{status} {claim}")

    def tool(name, args):
        return call(
            "POST",
            f"/v1/tools/{name}/invoke",
            {"agent": "worker-smoke", "task": code, "attempt": attempt, "arguments": args},
            token,
        )

    files = {
        "tests/__init__.py": "",
        "tests/integration/__init__.py": "",
        f"tests/integration/test_l2_{code.replace('-', '_')}.py": (
            "import unittest\n"
            "class L2(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            f"        self.assertEqual('{code}', '{code}')\n"
        ),
    }
    ok = True
    for path, content in files.items():
        status, out = tool("workspace_write_file", {"path": path, "content": content})
        ok = ok and status == 200 and out.get("status") == "ok"
    check("wrote tests only", ok)

    gates = {}
    for gate in ("compile", "unit", "integration"):
        status, out = tool("gate_run", {"gate": gate})
        output = (out or {}).get("output") or {}
        gates[gate] = output
        check(f"gate {gate} exit 0", output.get("exit") == 0, f"{status} {out}")
    status, out = tool("workspace_commit", {"message": f"{code}: L2 tests"})
    check("committed", status == 200 and out.get("status") == "ok", f"{status} {out}")
    status, out = tool("workspace_submit", {})
    submit = (out or {}).get("output") or {}
    check("submitted", status == 200 and bool(submit.get("candidate_ref")), f"{status} {out}")
    status, out = tool(
        "receipt_submit",
        {
            "receipt_id": f"R-{int(time.time()) % 10**9:09d}",
            "candidate_ref": submit.get("candidate_ref"),
            "base_commit": submit.get("base_commit"),
            "gates": [
                {"gate": name, "exit": value.get("exit"), "evidence_id": value.get("evidence_id")}
                for name, value in gates.items()
            ],
        },
    )
    check("L2 receipt accepted", status == 200 and out.get("status") == "ok", f"{status} {out}")
    receipt = (out or {}).get("output") or {}
    body = receipt.get("body") or {}
    _, detail = call("GET", f"/v1/tasks/{code}")
    stored = (detail.get("receipt") or {}).get("body") or {}
    check("classified L2", stored.get("risk_level") == "L2", stored.get("risk_level"))
    check(
        "receipt requires unit+integration",
        set(body.get("required_gates") or []) >= {"compile", "unit", "integration"},
        body.get("required_gates"),
    )
    receipt_id = next(
        (
            e.get("payload", {}).get("receipt_id")
            for e in detail.get("events", [])
            if e.get("type") == "receipt.submitted"
        ),
        None,
    )
    status, out = call(
        "POST",
        f"/v1/tasks/{code}/reviews",
        {"receipt_id": receipt_id, "decision": "accept", "findings": [], "reviewer": "coordinator-1"},
        ADMIN,
    )
    check("review accepted", status == 200, f"{status} {out}")
    status, out = call("POST", "/v1/merges", {"task": code}, ADMIN)
    check("merge enqueued", status == 201, f"{status} {out}")
    merge_id = (out or {}).get("merge_id")
    merged = False
    for _ in range(30):
        status, out = call("POST", f"/v1/merges/{merge_id}/process", None, ADMIN)
        if status == 200 and out.get("state") == "merged":
            merged = True
            break
        _, detail = call("GET", f"/v1/tasks/{code}")
        if detail.get("state") == "merged":
            merged = True
            break
        time.sleep(2)
    check("L2 merged without approval", merged, f"{status} {out}")
    _, approvals = call("GET", "/v1/approvals?pending=false", None, ADMIN)
    check(
        "no approval was required",
        not any(a.get("task") == code and a.get("action") == "merge" for a in (approvals or [])),
    )
    print(f"result={'OK' if FAIL == 0 else 'FAIL'} pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
