"""Reference HTTP client for the MACP agent communication protocol.

This script is a real client: it never writes the database directly. It
creates a T-9xxx task, dispatches it, claims it, sends turn events, submits a
receipt and asks the coordinator to accept it.

Usage:
    python agents/agent_smoke.py
    MACP_BASE=http://macp.example.com:8088 python agents/agent_smoke.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.getenv("MACP_BASE", "http://api:8000").rstrip("/")
WORKER = "worker-smoke"
TOKEN = os.getenv("MACP_WRITE_TOKEN", "").strip()
if not TOKEN and os.getenv("MACP_WRITE_TOKEN_FILE"):
    token_path = os.getenv("MACP_WRITE_TOKEN_FILE", "")
    if os.path.exists(token_path):
        TOKEN = open(token_path, encoding="utf-8").read().strip()
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"


def request(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {raw}") from exc


def main() -> None:
    code = f"T-9{int(time.time()) % 100000:05d}"
    receipt_id = f"R-{int(time.time() * 1000) % 10**9:09d}"
    print(f"task={code} receipt={receipt_id}")

    print("1. create task")
    status, task = request(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": "Agent protocol smoke task",
            "workstream": "stream-core",
            "kind": "slice",
            "priority": "P1",
        },
    )
    print("  ", status, task)

    print("2. dispatch to worker-e")
    status, offer = request("POST", f"/v1/tasks/{code}/dispatch", {"agent": WORKER})
    print("  ", status, offer)

    print("3. worker long-poll/next")
    status, assignment = request("GET", f"/v1/assignments/next?agent={WORKER}")
    print("  ", status, assignment)
    if not assignment:
        raise SystemExit("no assignment offered")
    if assignment["task"] != code:
        print("   next returned another task; using dispatch assignment instead")
    assignment = {"assignment_id": offer["assignment_id"]}

    print("4. claim")
    status, claim = request("POST", f"/v1/assignments/{assignment['assignment_id']}/claim")
    agent_token = claim["token"]["value"]
    print("  ", status, {"task": claim["task"], "attempt": claim["attempt"], "token_id": claim["token"]["id"]})

    def tool(name: str, args: dict):
        status, out = request(
            "POST",
            f"/v1/tools/{name}/invoke",
            {"agent": WORKER, "task": code, "attempt": claim["attempt"], "arguments": args},
            agent_token,
        )
        return status, (out or {}).get("output") or out

    content = "# " + code + "\ndef add(a, b):\n    return a + b\n"
    tool("workspace_write_file", {"path": "solution.py", "content": content})
    _, gate = tool("gate_run", {"gate": "compile"})
    tool("workspace_commit", {"message": f"{code}: protocol smoke"})
    _, submit = tool("workspace_submit", {})

    print("5. heartbeat")
    status, beat = request("POST", f"/v1/assignments/{assignment['assignment_id']}/heartbeat")
    print("  ", status, beat)

    print("6. turn events")
    print(
        "  ",
        request(
            "POST",
            f"/v1/agents/{WORKER}/events",
            {"type": "turn.started", "payload": {"task": code, "attempt": 1}},
        ),
    )
    print(
        "  ",
        request(
            "POST",
            f"/v1/agents/{WORKER}/events",
            {"type": "turn.ended", "payload": {"task": code, "attempt": 1, "tokens": 1200}},
        ),
    )

    print("7. submit receipt")
    status, receipt = request(
        "POST",
        f"/v1/tasks/{code}/receipts",
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
            "deviations": [],
            "pending_validation": [],
            "next_suggestion": None,
        },
        agent_token,
    )
    print("  ", status, receipt)

    print("8. coordinator review")
    status, review = request(
        "POST",
        f"/v1/tasks/{code}/reviews",
        {
            "receipt_id": receipt_id,
            "decision": "accept",
            "findings": [],
            "reviewer": "coordinator-1",
        },
    )
    print("  ", status, review)

    print("9. final task")
    status, detail = request("GET", f"/v1/tasks/{code}")
    print("  ", status, {"state": detail["state"], "assignee": detail["assignee"]})
    print("   recent events:", [event["type"] for event in detail["events"][:8]])


if __name__ == "__main__":
    main()
