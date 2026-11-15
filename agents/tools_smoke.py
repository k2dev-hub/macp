"""Exercise every MACP tool against the live environment.

Run on the lab host with the write token:
    MACP_WRITE_TOKEN=... MACP_BASE=http://macp.example.com:8088 python3 agents/tools_smoke.py
"""

from __future__ import annotations

import json
import time
import uuid

from .common import request

WORKER = "worker-smoke"


def must(status: int, body, label: str):
    if status not in (200, 201, 202):
        raise SystemExit(f"{label} failed: {status} {body}")
    print(f"OK {label}: {status}")
    return body


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
    must(status, body, f"tool {tool}")
    if body.get("status") != "ok":
        raise SystemExit(f"tool {tool} returned error: {body}")
    print("  ", json.dumps(body.get("output"), ensure_ascii=False)[:500])
    return body["output"]


def setup_task(suffix: str):
    code = f"T-9{int(time.time() * 1000) % 100000}{1 if suffix == 'a' else 2}"
    must(
        *request(
            "POST",
            "/v1/tasks",
            {
                "code": code,
                "title": f"Tool smoke {suffix}",
                "workstream": "stream-core",
                "kind": "slice",
                "priority": "P1",
            },
        ),
        f"create {code}",
    )
    offer = must(
        *request("POST", f"/v1/tasks/{code}/dispatch", {"agent": WORKER}),
        f"dispatch {code}",
    )
    must(
        *request("POST", f"/v1/assignments/{offer['assignment_id']}/claim"),
        f"claim {code}",
    )
    return code, offer["assignment_id"]


def main():
    task, _ = setup_task("a")
    invoke(task, "macp_task_get_work_order", {})
    invoke(task, "repo_list", {})
    invoke(task, "repo_read", {"path": "README.md"})
    invoke(task, "repo_search", {"query": "add"})
    invoke(
        task,
        "workspace_write_file",
        {"path": "solution.py", "content": "def add(a, b):\n    return a + b\n"},
    )
    patch = """diff --git a/solution.py b/solution.py
--- a/solution.py
+++ b/solution.py
@@ -1,2 +1,3 @@
 def add(a, b):
     return a + b
+# patched by tools_smoke
"""
    invoke(task, "workspace_apply_patch", {"patch": patch})
    invoke(task, "shell_run", {"platform": "sandbox", "argv": ["python3", "-m", "py_compile", "solution.py"]})
    invoke(task, "git_status", {})
    invoke(task, "git_diff", {"mode": "worktree"})
    invoke(task, "workspace_commit", {"message": "Add patched solution"})
    submit = invoke(task, "workspace_submit", {})
    gate = invoke(task, "gate_run", {"gate": "compile"})
    receipt = invoke(
        task,
        "receipt_submit",
        {
            "receipt_id": f"R-{int(time.time() * 1000) % 10**9:09d}",
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
    )
    receipt_id = receipt["body"]["receipt_id"]
    must(
        *request(
            "POST",
            f"/v1/tasks/{task}/reviews",
            {
                "receipt_id": receipt_id,
                "decision": "accept",
                "findings": [],
                "reviewer": "coordinator-1",
            },
        ),
        f"review {task}",
    )

    blocked_task, blocked_assignment = setup_task("b")
    invoke(
        blocked_task,
        "task_block",
        {
            "reason": "tools_smoke block path",
            "options": ["retry", "reduce scope", "handoff"],
        },
    )
    status, detail = request("GET", f"/v1/tasks/{blocked_task}")
    must(status, detail, f"read blocked {blocked_task}")
    assert detail["state"] == "blocked", detail["state"]
    must(
        *request("POST", f"/v1/assignments/{blocked_assignment}/release"),
        f"release blocked {blocked_task}",
    )
    print("OK blocked state:", blocked_task)


if __name__ == "__main__":
    main()
