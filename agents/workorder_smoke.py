"""Negative/positive checks for Work Order linter, write surface and acceptance."""

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
    print(tool, status, json.dumps(body, ensure_ascii=False)[:300])
    return status, body


def main() -> None:
    reset_worker()
    stamp = int(time.time() * 1000) % 100000
    bad = f"T-9{stamp}0"
    good = f"T-9{stamp}1"

    status, body = request(
        "POST",
        "/v1/tasks",
        {
            "code": bad,
            "title": "bad work order",
            "workstream": "stream-core",
            "kind": "slice",
            "priority": "P1",
            "work_order": {"write_surface": ["**"]},
        },
    )
    assert status == 422, (status, body)
    print("PASS invalid work order -> 422")

    status, body = request(
        "POST",
        "/v1/tasks",
        {
            "code": good,
            "title": "work order negative test",
            "workstream": "stream-core",
            "kind": "slice",
            "priority": "P1",
            "work_order": {
                "write_surface": ["src/**"],
                "acceptance": [{"gate": "compile", "required": True}],
            },
        },
    )
    assert status == 201, (status, body)
    print("PASS valid work order -> 201", body.get("work_order_hash"))

    status, offer = request(
        "POST", f"/v1/tasks/{good}/dispatch", {"agent": WORKER}
    )
    assert status == 201, (status, offer)
    status, _ = request(
        "POST", f"/v1/assignments/{offer['assignment_id']}/claim"
    )
    assert status == 200, status

    status, body = invoke(
        good,
        "workspace_write_file",
        {"path": "solution.py", "content": "x = 1\n"},
    )
    assert body.get("status") == "error" and "write_surface" in body["error"]["message"]

    status, body = invoke(
        good,
        "workspace_write_file",
        {"path": "src/ok.py", "content": "x = 1\n"},
    )
    assert body.get("status") == "ok", body

    status, body = invoke(
        good,
        "receipt_submit",
        {
            "receipt_id": "R-123456",
            "gates": [{"gate": "unit", "exit": 0}],
        },
    )
    assert body.get("status") == "error" and "acceptance gates" in body["error"]["message"]

    request("POST", f"/v1/assignments/{offer['assignment_id']}/release")
    print("PASS release")


if __name__ == "__main__":
    main()
