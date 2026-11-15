"""Verify single_writer hotfile lease conflict and release on merge."""

from __future__ import annotations

import json
import time
import uuid

from .common import request

WORKER = "worker-smoke"
WO = {
    "write_surface": ["solution.py", "shared/**"],
    "acceptance": [{"gate": "compile", "required": True}],
}


def reset_worker() -> None:
    status, rows = request("GET", f"/v1/assignments?state=all&agent={WORKER}")
    if status != 200:
        return
    for row in rows:
        if row.get("state") in ("offered", "claimed", "active", "submitted"):
            request("POST", f"/v1/assignments/{row['id']}/release")


def invoke(task: str, tool: str, arguments: dict):
    status, body = request(
        "POST",
        f"/v1/tools/{tool}/invoke",
        {
            "tool_call_id": f"tc_{uuid.uuid4().hex[:10]}",
            "agent": WORKER,
            "task": task,
            "attempt": 1,
            "arguments": arguments,
        },
    )
    return status, body


def create_task(suffix: str):
    code = f"T-9{int(time.time() * 1000) % 100000}{suffix}"
    status, body = request(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": f"lease smoke {suffix}",
            "workstream": "stream-core",
            "kind": "slice",
            "priority": "P1",
            "work_order": WO,
        },
    )
    assert status == 201, (status, body)
    status, offer = request(
        "POST", f"/v1/tasks/{code}/dispatch", {"agent": WORKER}
    )
    assert status == 201, (status, offer)
    status, _ = request("POST", f"/v1/assignments/{offer['assignment_id']}/claim")
    assert status == 200, status
    return code, offer["assignment_id"]


def main() -> None:
    reset_worker()
    a, a_assignment = create_task("1")
    invoke(
        a,
        "workspace_write_file",
        {"path": "solution.py", "content": f"# lease smoke A {a}\ndef multiply(a, b):\n    return a * b\n"},
    )
    gate = invoke(a, "gate_run", {"gate": "compile"})[1]["output"]
    status, body = invoke(
        a, "workspace_write_file", {"path": "shared/a.txt", "content": "a\n"}
    )
    assert body.get("status") == "ok", body
    invoke(a, "workspace_commit", {"message": "lease A"})
    submit = invoke(a, "workspace_submit", {})[1]["output"]
    receipt = f"R-{int(time.time() * 1000) % 10**9:09d}"
    invoke(
        a,
        "receipt_submit",
        {
            "receipt_id": receipt,
            "candidate_ref": submit["candidate_ref"],
            "base_commit": submit["base_commit"],
            "gates": [{"gate": "compile", "exit": 0, "evidence_id": gate["evidence_id"]}],
            "pending_validation": [],
            "deviations": [],
        },
    )
    status, accepted = request(
        "POST",
        f"/v1/tasks/{a}/reviews",
        {"receipt_id": receipt, "decision": "accept", "findings": [], "reviewer": "coordinator-1"},
    )
    assert status == 200, (status, accepted)
    print("A accepted, lease held", a)

    b, b_assignment = create_task("2")
    status, body = invoke(
        b, "workspace_write_file", {"path": "shared/b.txt", "content": "b\n"}
    )
    print("B write:", body)
    assert body.get("status") == "error" and "lease conflict" in body["error"]["message"], body
    request("POST", f"/v1/assignments/{b_assignment}/release")

    status, queued = request("POST", "/v1/merges", {"task": a})
    assert status == 201, (status, queued)
    status, merged = request("POST", f"/v1/merges/{queued['merge_id']}/process")
    assert status == 200 and merged["state"] == "merged", (status, merged)
    print("A merged, lease released", merged["merge_commit"])

    c, c_assignment = create_task("3")
    status, body = invoke(
        c, "workspace_write_file", {"path": "shared/c.txt", "content": "c\n"}
    )
    print("C write:", body)
    assert body.get("status") == "ok", body
    request("POST", f"/v1/assignments/{c_assignment}/release")
    print("PASS lease conflict/release")


if __name__ == "__main__":
    main()
