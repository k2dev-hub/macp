"""L2/L3 gate smoke: risk classification, security gate, merge approval."""

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
    code = f"T-7{int(time.time()) % 10**6:06d}"
    status, created = call(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": "L3 deploy change",
            "workstream": "stream-infra",
            "priority": "P2",
            "budget": {"max_tool_calls": 40, "max_model_calls": 8, "max_cost": 2},
            "work_order": {
                "write_surface": ["solution.py", "deploy/**", "tests/**"],
                "acceptance": [{"gate": "compile", "required": True, "level": "L1"}],
            },
        },
        ADMIN,
    )
    check("L3 task created", status == 201, f"{status} {created}")
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
        "solution.py": f"# {code}\ndef deploy_flag():\n    return True\n",
        "deploy/release.yaml": f"replicas: 1\nimage: macp:test\nannotation: {code}\n",
        "tests/__init__.py": "",
        "tests/integration/__init__.py": "",
        "tests/integration/test_ok.py": (
            "import unittest\n"
            "class Smoke(unittest.TestCase):\n"
            "    def test_true(self):\n"
            "        self.assertTrue(True)\n"
        ),
    }
    ok = True
    for path, content in files.items():
        status, out = tool("workspace_write_file", {"path": path, "content": content})
        ok = ok and status == 200 and out.get("status") == "ok"
    check("wrote deploy + tests", ok)

    gates = {}
    for gate in ("compile", "unit", "integration", "security"):
        status, out = tool("gate_run", {"gate": gate})
        output = (out or {}).get("output") or {}
        gates[gate] = output
        check(f"gate {gate} exit 0", output.get("exit") == 0, f"{status} {out}")
    status, out = tool(
        "workspace_commit", {"message": f"{code}: L3 deploy change"}
    )
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
    check("L3 receipt accepted", status == 200 and out.get("status") == "ok", f"{status} {out}")
    receipt = (out or {}).get("output") or {}
    receipt_id = ((receipt.get("body") or {}).get("receipt_id")) or receipt.get("receipt_id")
    detail_status, detail = call("GET", f"/v1/tasks/{code}")
    receipt_body = next(
        (
            e.get("payload") or {}
            for e in (detail or {}).get("events", [])
            if e.get("type") == "receipt.submitted"
        ),
        {},
    )
    receipt_id = receipt_body.get("receipt_id") or receipt_id
    check("receipt id recovered", bool(receipt_id), str(receipt_body))
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
    status, out = call("POST", f"/v1/merges/{merge_id}/process", None, ADMIN)
    check(
        "merge blocked pending approval",
        status == 409 and (out or {}).get("detail", {}).get("code") == "approval_required",
        f"{status} {out}",
    )
    status, approval = call(
        "POST",
        "/v1/approvals",
        {
            "task": code,
            "action": "merge",
            "risk": "high",
            "options": ["approve", "deny"],
            "default_option": "deny",
        },
        ADMIN,
    )
    check("approval requested", status == 201, f"{status} {approval}")
    status, out = call(
        "POST",
        f"/v1/approvals/{approval['id']}/decide",
        {"decision": "approved", "rationale": "smoke test"},
        ADMIN,
    )
    check("approval decided", status == 200, f"{status} {out}")
    merged = False
    out = {}
    for _ in range(8):
        status, out = call("POST", f"/v1/merges/{merge_id}/process", None, ADMIN)
        merged = status == 200 and out.get("state") == "merged"
        if merged:
            break
        _, detail = call("GET", f"/v1/tasks/{code}")
        if detail.get("state") == "merged":
            merged = True
            break
        time.sleep(1)
    check("merge completed", merged, f"{status} {out}")
    print(f"result={'OK' if FAIL == 0 else 'FAIL'} pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
