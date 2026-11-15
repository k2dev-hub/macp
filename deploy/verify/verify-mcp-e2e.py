#!/usr/bin/env python3
"""End-to-end through the MCP gateway: claim -> write -> gate -> submit -> merged.

Drives the gateway over JSON-RPC (the same path Claude Code / Cursor take), then
performs the human review/merge over REST as the admin, and finally checks the
audit chain recorded channel=mcp.

Run on macp.example.com (needs the admin token file).
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("MCP_BASE", "https://localhost:9443")
ADMIN = open("/opt/macp/deploy/secrets/write_token", encoding="utf-8").read().strip()
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
CODE = f"T-22{int(time.time()) % 10000:04d}"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:180]) if detail else ""))
    if not ok:
        failures.append(name)


def http(method: str, url: str, body=None, token: str | None = None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=600, context=CTX) as response:
            raw = response.read().decode()
            return response.status, dict(response.headers), (
                json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, dict(exc.headers), (json.loads(raw) if raw.strip() else None)
        except json.JSONDecodeError:
            return exc.code, dict(exc.headers), raw


class Mcp:
    def __init__(self, token: str):
        self.token = token
        self.session = ""
        self.seq = 0

    def rpc(self, method: str, params: dict | None = None):
        self.seq += 1
        headers = {"MCP-Protocol-Version": "2025-06-18"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        status, resp_headers, body = http(
            "POST", BASE + "/mcp",
            {"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params or {}},
            token=self.token, headers=headers)
        session = resp_headers.get("Mcp-Session-Id") or resp_headers.get("mcp-session-id")
        if session:
            self.session = session
        return status, body

    def call(self, name: str, arguments: dict | None = None) -> dict:
        status, body = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        result = (body or {}).get("result") or {}
        text = (result.get("content") or [{}])[0].get("text", "")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"raw": text}
        payload["_isError"] = bool(result.get("isError"))
        return payload


# ------------------------------------------------------------------ admin setup
status, _, tasks = http("GET", BASE + "/v1/tasks", token=ADMIN)
for task in tasks or []:
    if task.get("assignee") in ("worker-d",) and task.get("state") in (
            "ready", "assigned", "in_progress"):
        http("POST", BASE + f"/v1/tasks/{task['code']}/requeue", {"reason": "mcp e2e cleanup"},
             token=ADMIN)

status, _, created = http(
    "POST", BASE + "/v1/tasks",
    {"code": CODE, "title": "MCP e2e task", "workstream": "stream-infra", "kind": "infra",
     "work_order": {
         "write_surface": ["src/**", "tests/**"],
         "acceptance": [{"gate": "compile", "required": True, "level": "L1"}],
         "execution": {"profile": "local"},
     }},
    token=ADMIN)
check("admin creates the task", status in (200, 201), status)
status, _, dispatched = http("POST", BASE + f"/v1/tasks/{CODE}/dispatch", {"agent": "worker-d"},
                             token=ADMIN)
check("dispatch to worker-d", status in (200, 201), status)

scopes = ["mcp:connect", "mcp:write", "task:claim", "tool:invoke", "repo:read",
          "workspace:read", "workspace:write", "gate:run", "receipt:write",
          "task:heartbeat", "task:release", "agent:event", "doc:read"]
status, _, minted = http("POST", BASE + "/v1/admin/tokens",
                         {"agent": "worker-d", "scopes": scopes, "ttl_minutes": 60},
                         token=ADMIN)
TOKEN = (minted or {}).get("value", "")
check("mint a write-capable MCP token", status == 201 and bool(TOKEN), status)
if not TOKEN:
    print("FAILURES:", failures)
    sys.exit(1)

# --------------------------------------------------------------------- MCP flow
mcp = Mcp(TOKEN)
status, body = mcp.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
check("initialize", status == 200 and bool(mcp.session), mcp.session)

status, body = mcp.rpc("tools/list", {})
names = {t["name"] for t in ((body or {}).get("result") or {}).get("tools", [])}
check("write tools are listed", {"macp_file_write", "macp_gate_run", "macp_task_next"} <= names,
      sorted(names)[:12])

claimed = mcp.call("macp_task_next")
check("macp_task_next claims an assignment", not claimed["_isError"], claimed)
check("claim binds the task", claimed.get("task") == CODE, claimed.get("task"))
work_order = claimed.get("work_order") or {}
check("claim returns the work order",
      isinstance(work_order, dict) and bool(work_order.get("acceptance") or work_order.get("task")),
      str(work_order)[:120])

written = mcp.call("macp_file_write", {
    "path": "src/mcp_e2e.py",
    "content": f'"""written through the MCP gateway ({CODE})."""\n\n\ndef add(a, b):\n    return a + b\n',
})
check("macp_file_write", not written["_isError"], written)

gate = mcp.call("macp_gate_run", {"gate": "compile"})
check("macp_gate_run returns a job", bool(gate.get("job_id")), gate)
job_id = gate.get("job_id", "")
waited = mcp.call("macp_job_wait", {"job_id": job_id, "timeout_seconds": 30})
check("job_wait reports success", waited.get("status") == "succeeded", waited)
evidence_id = ((waited.get("result") or {}).get("evidence_id")) or ""
check("gate produced an evidence id", bool(evidence_id), evidence_id)
gate_exit = int(((waited.get("result") or {}).get("exit")) or 0)
check("gate exit is 0", gate_exit == 0, gate_exit)

committed = mcp.call("macp_git_commit", {"message": f"{CODE}: written via MCP"})
check("macp_git_commit", not committed["_isError"], committed)
submitted = mcp.call("macp_git_submit")
check("macp_git_submit", not submitted["_isError"], submitted)

receipt = mcp.call("macp_receipt_submit", {
    "receipt_id": f"R-{CODE.replace('-', '')}",
    "candidate_ref": submitted.get("candidate_ref"),
    "base_commit": submitted.get("base_commit"),
    "gates": [{"gate": "compile", "exit": gate_exit, "evidence_id": evidence_id}],
})
check("macp_receipt_submit", not receipt["_isError"], receipt)

# binding must be released so the session can take the next task
status, _, body = http("GET", BASE + "/v1/tasks/" + CODE, token=ADMIN)
check("task moved to review", (body or {}).get("state") == "review", (body or {}).get("state"))

# ------------------------------------------------------- human review and merge
rid = receipt.get("receipt_id") or (receipt.get("body") or {}).get("receipt_id")
status, _, review = http("POST", BASE + f"/v1/tasks/{CODE}/reviews",
                         {"decision": "accept", "receipt_id": rid}, token=ADMIN)
check("review accepted", status == 200, review)
status, _, merge = http("POST", BASE + "/v1/merges", {"task": CODE}, token=ADMIN)
merge_id = (merge or {}).get("id") or (merge or {}).get("merge_id")
status, _, processed = http("POST", BASE + f"/v1/merges/{merge_id}/process", token=ADMIN)
check("merge processed", status == 200, processed)

deadline = time.time() + 120
state = ""
while time.time() < deadline:
    status, _, task = http("GET", BASE + f"/v1/tasks/{CODE}", token=ADMIN)
    state = (task or {}).get("state", "")
    if state == "merged":
        break
    time.sleep(3)
check("task merged", state == "merged", state)

# ------------------------------------------------------------------------ audit
status, _, events = http("GET", BASE + "/v1/events?limit=200", token=ADMIN)
called = [e for e in (events or [])
          if e.get("type") == "tool.called"
          and (e.get("payload") or {}).get("channel") == "mcp"]
check("tool.called records channel=mcp", len(called) >= 4, len(called))

status, _, chain = http("GET", BASE + "/v1/audit/chain?limit=5000", token=ADMIN)
check("audit chain still ok", (chain or {}).get("ok") is True, (chain or {}).get("broken_links"))

print()
print(f"TASK={CODE}")
print("FAILURES:", failures or "none")
sys.exit(1 if failures else 0)
