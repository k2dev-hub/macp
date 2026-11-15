"""Verify reject -> attempt 2 -> rework -> accept."""

from __future__ import annotations

import json
import time
import uuid

from .common import request

WORKER = "worker-smoke"


def reset_worker() -> None:
    status, rows = request("GET", f"/v1/assignments?state=all&agent={WORKER}")
    if status != 200:
        return
    for row in rows:
        if row.get("state") in ("offered", "claimed", "active", "submitted"):
            request("POST", f"/v1/assignments/{row['id']}/release")


def invoke(task: str, tool: str, arguments: dict, attempt: int):
    status, body = request(
        "POST",
        f"/v1/tools/{tool}/invoke",
        {
            "tool_call_id": f"tc_{uuid.uuid4().hex[:10]}",
            "agent": WORKER,
            "task": task,
            "attempt": attempt,
            "arguments": arguments,
        },
    )
    if status != 200 or body.get("status") != "ok":
        raise SystemExit(f"{tool} failed: {status} {body}")
    print(tool, "ok")
    return body["output"]


def work(task: str, attempt: int) -> str:
    invoke(
        task,
        "workspace_write_file",
        {"path": "solution.py", "content": f"def multiply(a, b):\n    return a * b\n# rework attempt {attempt}\n"},
        attempt,
    )
    gate = invoke(task, "gate_run", {"gate": "compile"}, attempt)
    invoke(task, "workspace_commit", {"message": f"attempt {attempt}"}, attempt)
    submit = invoke(task, "workspace_submit", {}, attempt)
    receipt_id = f"R-{int(time.time() * 1000) % 10**9:09d}"
    invoke(
        task,
        "receipt_submit",
        {
            "receipt_id": receipt_id,
            "candidate_ref": submit["candidate_ref"],
            "base_commit": submit["base_commit"],
            "gates": [
                {
                    "gate": "compile",
                    "exit": gate["exit"],
                    "evidence_id": gate["evidence_id"],
                }
            ],
            "pending_validation": [],
            "deviations": [],
        },
        attempt,
    )
    return receipt_id


def main() -> None:
    reset_worker()
    code = f"T-9{int(time.time() * 1000) % 100000}"[:7]
    status, _ = request(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": "rework smoke",
            "workstream": "stream-core",
            "kind": "slice",
            "priority": "P1",
        },
    )
    assert status == 201, status
    status, offer = request(
        "POST", f"/v1/tasks/{code}/dispatch", {"agent": WORKER}
    )
    assert status == 201, (status, offer)
    request("POST", f"/v1/assignments/{offer['assignment_id']}/claim")

    receipt1 = work(code, 1)
    status, rejected = request(
        "POST",
        f"/v1/tasks/{code}/reviews",
        {
            "receipt_id": receipt1,
            "decision": "reject",
            "findings": [
                {
                    "file": "solution.py",
                    "line": 1,
                    "code": "test",
                    "expected": "add a docstring and retry",
                    "must_fix": True,
                }
            ],
            "reviewer": "coordinator-1",
        },
    )
    assert status == 200, (status, rejected)
    print("rejected", rejected)
    assert rejected["next_attempt"] == 2 and rejected["assignment_id"]

    status, detail = request("GET", f"/v1/tasks/{code}")
    assert detail["state"] in ("assigned", "in_progress") and detail["current_attempt"] == 2, detail

    status, claimed = request(
        "POST", f"/v1/assignments/{rejected['assignment_id']}/claim"
    )
    assert status == 200, (status, claimed)
    receipt2 = work(code, 2)
    status, accepted = request(
        "POST",
        f"/v1/tasks/{code}/reviews",
        {
            "receipt_id": receipt2,
            "decision": "accept",
            "findings": [],
            "reviewer": "coordinator-1",
        },
    )
    assert status == 200 and accepted["state"] == "accepted", (status, accepted)
    status, final = request("GET", f"/v1/tasks/{code}")
    print("final", final["state"], final["current_attempt"])
    print([e["type"] for e in final["events"][:12]])


if __name__ == "__main__":
    main()
