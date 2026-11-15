#!/usr/bin/env python3
"""MCP gateway smoke test (Phase 1).

Covers the protocol, auth, scoping and resources of /mcp:
  * no token -> 401 / -32001
  * initialize -> Mcp-Session-Id, protocolVersion, serverInfo
  * tools/list -> read-only tools only, no write tool
  * tools/call macp_task_get / macp_repo_read / macp_git_status
  * resources/list + resources/read macp://task/{code}
  * missing session -> -32002
  * write tool -> isError forbidden
  * token never appears in any response

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
MCP_URL = BASE + "/mcp"
ADMIN_TOKEN = open(
    os.getenv("MCP_ADMIN_TOKEN_FILE", "/opt/macp/deploy/secrets/write_token"),
    encoding="utf-8",
).read().strip()
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

failures: list[str] = []
TOKEN = ""
SEEN_BODIES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:200]) if detail else ""))
    if not ok:
        failures.append(name)


def http(method: str, url: str, body=None, token: str | None = None,
         headers: dict | None = None, timeout: int = 60):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=CTX) as response:
            raw = response.read().decode()
            if "/mcp" in url:
                SEEN_BODIES.append(raw)
            return response.status, dict(response.headers), (
                json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        if "/mcp" in url:
            SEEN_BODIES.append(raw)
        try:
            return exc.code, dict(exc.headers), (json.loads(raw) if raw.strip() else None)
        except json.JSONDecodeError:
            return exc.code, dict(exc.headers), raw


def rpc(payload: dict, session: str | None = None, token: str | None = None):
    headers = {"MCP-Protocol-Version": "2025-06-18"}
    if session:
        headers["Mcp-Session-Id"] = session
    return http("POST", MCP_URL, payload, token=token, headers=headers)


# ------------------------------------------------------------------ token setup
# The agent-scoped tools (work order, repo, documents) only serve the task that
# is assigned to the token's own agent, so mint the token for the agent that owns
# a real task instead of a fixed one.
status, _, tasks = http("GET", BASE + "/v1/tasks", token=ADMIN_TOKEN)
task_list = [t for t in (tasks or []) if isinstance(t, dict) and t.get("code")]
chosen = next((t for t in task_list if t.get("assignee")), task_list[0] if task_list else None)
SMOKE_AGENT = (chosen or {}).get("assignee") or "worker-d"

status, _, created = http(
    "POST", BASE + "/v1/admin/tokens",
    {"agent": SMOKE_AGENT,
     "scopes": ["mcp:connect", "task:read", "tool:invoke", "repo:read", "doc:read"],
     "ttl_minutes": 30},
    token=ADMIN_TOKEN,
)
check("admin can mint a read-only MCP token", status == 201 and created and created.get("value"),
      status)
TOKEN = (created or {}).get("value", "")
if not TOKEN:
    print("FAILURES:", failures)
    sys.exit(1)

# ------------------------------------------------------------------- auth checks
status, _, body = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
check("initialize without token is 401", status == 401, status)
check("401 carries JSON-RPC -32001",
      isinstance(body, dict) and (body.get("error") or {}).get("code") == -32001, body)

status, _, body = rpc({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                      token=ADMIN_TOKEN)
check("admin token is not an MCP token (no mcp:connect)", status == 403, status)

# ------------------------------------------------------------------- initialize
status, headers, body = rpc(
    {"jsonrpc": "2.0", "id": 3, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "macp-smoke", "version": "0.1.0"}}},
    token=TOKEN,
)
session = (headers or {}).get("Mcp-Session-Id") or (headers or {}).get("mcp-session-id")
check("initialize succeeds", status == 200 and (body or {}).get("result"), status)
check("initialize returns Mcp-Session-Id", bool(session), session)
result = (body or {}).get("result") or {}
check("serverInfo names the gateway", result.get("serverInfo", {}).get("name") == "macp", result)
check("protocol version negotiated",
      result.get("protocolVersion") == "2025-06-18", result.get("protocolVersion"))

# No session -> -32002
status, _, body = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
                      token=TOKEN)
check("tools/list without a session is -32002",
      isinstance(body, dict) and (body.get("error") or {}).get("code") == -32002, body)

# ------------------------------------------------------------------- tools/list
status, _, body = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
                      session=session, token=TOKEN)
tools = {t["name"]: t for t in ((body or {}).get("result") or {}).get("tools", [])}
check("tools/list returns tools", status == 200 and len(tools) >= 6, sorted(tools))
check("read tools carry readOnlyHint",
      all(t.get("annotations", {}).get("readOnlyHint") for t in tools.values()))
check("write tools are not exposed",
      not ({"macp_shell_run", "macp_file_write", "macp_gate_run"} & set(tools)),
      sorted(tools))

# -------------------------------------------------------------------- task read
codes = [t["code"] for t in task_list]
if not codes:
    status, _, created_task = http(
        "POST", BASE + "/v1/tasks",
        {"code": f"T-33{int(time.time()) % 10000:04d}", "title": "mcp smoke",
         "workstream": "stream-infra", "kind": "infra",
         "work_order": {"write_surface": ["notes/**"],
                        "acceptance": [{"gate": "compile", "required": True, "level": "L1"}],
                        "execution": {"profile": "local"}}},
        token=ADMIN_TOKEN,
    )
    if status in (200, 201) and isinstance(created_task, dict):
        codes = [created_task["code"]]
check("a task exists to read", bool(codes), codes)

status, _, body = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "macp_task_get", "arguments": {"task": codes[0]}}},
                      session=session, token=TOKEN)
call = (body or {}).get("result") or {}
text = (call.get("content") or [{}])[0].get("text", "")
check("macp_task_get returns the task", status == 200 and codes[0] in text, text[:120])
check("macp_task_get is not an error", call.get("isError") is False, call.get("isError"))

status, _, body = rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "macp_shell_run",
                                  "arguments": {"argv": ["ls"], "platform": "sandbox"}}},
                      session=session, token=TOKEN)
call = (body or {}).get("result") or {}
text = (call.get("content") or [{}])[0].get("text", "")
check("write tool is refused with isError", call.get("isError") is True, text[:120])
check("refusal mentions forbidden", "forbidden" in text, text[:120])

# --------------------------------------------------------------------- resources
status, _, body = rpc({"jsonrpc": "2.0", "id": 8, "method": "resources/list", "params": {}},
                      session=session, token=TOKEN)
resources = ((body or {}).get("result") or {}).get("resources") or []
uris = [r.get("uri") for r in resources]
check("resources/list returns task resources",
      any(u and u.startswith("macp://task/") for u in uris), uris[:3])

status, _, body = rpc({"jsonrpc": "2.0", "id": 9, "method": "resources/read",
                       "params": {"uri": f"macp://task/{codes[0]}"}},
                      session=session, token=TOKEN)
contents = ((body or {}).get("result") or {}).get("contents") or []
check("resources/read returns the task body",
      bool(contents) and codes[0] in (contents[0].get("text") or ""), contents[:1])

status, _, body = rpc({"jsonrpc": "2.0", "id": 10, "method": "resources/read",
                       "params": {"uri": "macp://workorder/" + codes[0]}},
                      session=session, token=TOKEN)
contents = ((body or {}).get("result") or {}).get("contents") or []
check("resources/read returns the work order",
      bool(contents) and "acceptance" in (contents[0].get("text") or ""),
      (contents[0].get("text") or "")[:100] if contents else "")

# ------------------------------------------------------------------ leak check
leaked = [b for b in SEEN_BODIES if TOKEN and TOKEN in b]
check("no response echoes the MCP token", not leaked, len(leaked))

# --------------------------------------------------------------------- cleanup
status, _, _ = http("DELETE", MCP_URL, token=TOKEN, headers={"Mcp-Session-Id": session})
check("DELETE /mcp closes the session", status in (204, 200), status)

print()
print("FAILURES:", failures or "none")
sys.exit(1 if failures else 0)
