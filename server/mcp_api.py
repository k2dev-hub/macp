"""MACP MCP gateway (Phase 2: task binding + write tools).

The gateway is an adapter, not a second implementation: every tool call is
forwarded to the existing ``POST /v1/tools/{name}/invoke`` so scopes,
write-surface checks, gates, evidence and the event chain keep working exactly as
they do for the OpenAI tool loop.

Transport: MCP "Streamable HTTP" over JSON-RPC 2.0 on a single ``/mcp`` path.
Sessions live in memory (single replica) and hold, for a bound attempt, the task
token the API minted on claim.  Tokens are never logged or returned.

Phase 2 adds:
  * macp_task_next: claim an assignment and bind the session to it;
  * write tools (file_write/patch, shell_run, gate_run, git_commit/submit,
    receipt_submit, task_block) requiring mcp:write and a bound task;
  * asynchronous gate jobs (macp_job_status/wait/cancel);
  * the binding is cleared once the receipt or a block is submitted.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from .tool_registry import TOOLS

API_BASE = os.getenv("MACP_INTERNAL_BASE", "http://api:8000").rstrip("/")
DEFAULT_PROJECT = os.getenv("MACP_MCP_PROJECT", "demo")
SESSION_TTL = int(os.getenv("MACP_MCP_SESSION_TTL_SECONDS", "14400"))
MAX_SESSIONS = int(os.getenv("MACP_MCP_MAX_SESSIONS", "200"))
REQUESTS_PER_MINUTE = int(os.getenv("MACP_MCP_RATE_LIMIT", "60"))
MAX_TOOL_OUTPUT = int(os.getenv("MACP_MCP_MAX_OUTPUT_CHARS", "20000"))
JOB_TTL = int(os.getenv("MACP_MCP_JOB_TTL_SECONDS", "86400"))
GATE_WAIT_SECONDS = int(os.getenv("MACP_MCP_GATE_WAIT_SECONDS", "30"))

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
SERVER_INFO = {"name": "macp", "version": "0.2.0"}

# name -> (underlying tool, needs task).  "" means a direct REST read.
READ_TOOLS: dict[str, tuple[str, bool]] = {
    "macp_task_get": ("", True),
    "macp_workorder_get": ("macp_task_get_work_order", True),
    "macp_repo_list": ("repo_list", True),
    "macp_repo_read": ("repo_read", True),
    "macp_repo_search": ("repo_search", True),
    "macp_document_list": ("task_document_list", True),
    "macp_document_read": ("task_document_read", True),
    "macp_git_status": ("git_status", True),
    "macp_git_diff": ("git_diff", True),
}
# name -> underlying write tool
WRITE_TOOLS: dict[str, str] = {
    "macp_file_write": "workspace_write_file",
    "macp_file_patch": "workspace_apply_patch",
    "macp_shell_run": "shell_run",
    "macp_gate_run": "gate_run",
    "macp_git_commit": "workspace_commit",
    "macp_git_submit": "workspace_submit",
    "macp_receipt_submit": "receipt_submit",
    "macp_task_block": "task_block",
}
TERMINAL_TOOLS = {"macp_receipt_submit", "macp_task_block"}
ASYNC_TOOLS = {"macp_gate_run"}

app = FastAPI(title="MACP MCP Gateway", version="0.2.0")


# --------------------------------------------------------------------------- http
def _api_call(
    method: str, path: str, token: str, body: Any = None, project: str | None = None,
    timeout: int = 60,
) -> tuple[int, Any]:
    """Blocking call to the internal API; run it with asyncio.to_thread."""

    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(API_BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-MACP-Project", project or DEFAULT_PROJECT)
    # Lets the API attribute the audit event to the MCP channel.
    request.add_header("X-MACP-Channel", "mcp")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw[:400]}
    except (urllib.error.URLError, TimeoutError) as exc:
        return 503, {"detail": f"api unreachable: {exc}"}


async def api_call(method: str, path: str, token: str, body: Any = None,
                   project: str | None = None, timeout: int = 60) -> tuple[int, Any]:
    return await asyncio.to_thread(_api_call, method, path, token, body, project, timeout)


# ------------------------------------------------------------------------ session
@dataclass
class Session:
    id: str
    token: str
    project: str
    agent: str | None
    kind: str
    scopes: set[str] = field(default_factory=set)
    expires_at: float = 0.0
    calls: list[float] = field(default_factory=list)
    task: str | None = None
    attempt: int = 1
    task_token: str | None = None

    @property
    def bound(self) -> bool:
        return bool(self.task and self.task_token)


SESSIONS: dict[str, Session] = {}


@dataclass
class Job:
    id: str
    session: str
    tool: str
    task: str | None
    status: str = "running"
    result: Any = None
    error: Any = None
    created: float = field(default_factory=time.time)
    finished: float | None = None


JOBS: dict[str, Job] = {}


def _prune() -> None:
    now = time.time()
    for key in [k for k, s in SESSIONS.items() if s.expires_at <= now]:
        SESSIONS.pop(key, None)
    for key in [k for k, j in JOBS.items() if j.finished and now - j.finished > JOB_TTL]:
        JOBS.pop(key, None)


def _new_session(token: str, me: dict) -> Session:
    _prune()
    if len(SESSIONS) >= MAX_SESSIONS:
        oldest = min(SESSIONS.values(), key=lambda s: s.expires_at)
        SESSIONS.pop(oldest.id, None)
    session = Session(
        id="mcp-" + secrets.token_urlsafe(18),
        token=token,
        project=str(me.get("project") or DEFAULT_PROJECT),
        agent=me.get("agent"),
        kind=str(me.get("kind") or "agent"),
        scopes=set(me.get("scopes") or []),
        expires_at=time.time() + SESSION_TTL,
    )
    SESSIONS[session.id] = session
    return session


def _allows(scopes: set[str], scope: str) -> bool:
    return scope in scopes or "*" in scopes


# -------------------------------------------------------------------- json-rpc
def rpc_ok(msg_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})


def rpc_err(msg_id: Any, code: int, message: str, data: Any = None,
            status: int = 200) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "error": error}, status_code=status)


def tool_result(payload: Any, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text[:MAX_TOOL_OUTPUT]}],
        "isError": is_error,
    }


def _schema_for(name: str, tool: str, read: bool) -> dict:
    source = next(
        (item["function"] for item in TOOLS if item["function"]["name"] == tool), None
    )
    params = json.loads(json.dumps((source or {}).get("parameters") or {}))
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    if read:
        params["properties"]["task"] = {
            "type": "string",
            "description": "MACP task code. Read tools take it explicitly until the "
                           "session is bound by macp_task_next.",
        }
        params["additionalProperties"] = True
    else:
        params["additionalProperties"] = False
    description = (source or {}).get("description") or f"MACP tool {tool}"
    if name == "macp_task_get":
        description = "Read a MACP task: state, assignee, work_order_hash, budget."
        params["required"] = ["task"]
    if name == "macp_gate_run":
        description = ("Run an acceptance gate in the task's execution profile. "
                       "Returns a job id; poll with macp_job_status or macp_job_wait.")
    return {
        "name": name,
        "description": description,
        "inputSchema": params,
        "annotations": (
            {"readOnlyHint": True} if read
            else {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}
        ),
    }


def _tool_catalog() -> list[dict]:
    catalog = [_schema_for(name, tool, True) for name, (tool, _) in READ_TOOLS.items()]
    catalog.append({
        "name": "macp_task_next",
        "description": ("Claim the next assignment for this agent and bind the session "
                        "to it. Returns the task, the Work Order and the budget."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": False},
    })
    catalog.extend(_schema_for(name, tool, False) for name, tool in WRITE_TOOLS.items())
    for name, description, schema in (
        ("macp_job_status", "Status of an async job.",
         {"type": "object", "required": ["job_id"],
          "properties": {"job_id": {"type": "string"}}, "additionalProperties": False}),
        ("macp_job_wait", "Wait up to timeout_seconds for an async job.",
         {"type": "object", "required": ["job_id"],
          "properties": {"job_id": {"type": "string"},
                         "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60}},
          "additionalProperties": False}),
        ("macp_job_cancel", "Best-effort cancel of an async job.",
         {"type": "object", "required": ["job_id"],
          "properties": {"job_id": {"type": "string"}}, "additionalProperties": False}),
    ):
        catalog.append({
            "name": name, "description": description, "inputSchema": schema,
            "annotations": {"readOnlyHint": name != "macp_job_cancel"},
        })
    return catalog


# ------------------------------------------------------------------------ methods
async def _initialize(msg_id: Any, params: dict, authorization: str | None) -> Response:
    if not authorization or not authorization.startswith("Bearer "):
        return rpc_err(msg_id, -32001, "unauthorized: bearer token required", status=401)
    token = authorization.removeprefix("Bearer ").strip()
    status, me = await api_call("GET", "/v1/auth/me", token)
    if status != 200 or not isinstance(me, dict):
        return rpc_err(msg_id, -32001, "unauthorized: invalid or expired token", status=401)
    if str(me.get("kind")) == "admin":
        # The design forbids using the admin token as an MCP token: adapters must
        # run with a project/agent-scoped credential.
        return rpc_err(msg_id, -32003,
                       "forbidden: admin tokens cannot be used as MCP tokens", status=403)
    scopes = set(me.get("scopes") or [])
    if not _allows(scopes, "mcp:connect"):
        return rpc_err(msg_id, -32003, "forbidden: token lacks mcp:connect",
                       data={"required_scope": "mcp:connect"}, status=403)

    requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
    version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
    session = _new_session(token, me)
    response = rpc_ok(
        msg_id,
        {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": SERVER_INFO,
        },
    )
    response.headers["Mcp-Session-Id"] = session.id
    return response


async def _tools_call(msg_id: Any, params: dict, session: Session) -> Response:
    name = str(params.get("name") or "")
    arguments = dict(params.get("arguments") or {})

    if name == "macp_task_next":
        return await _task_next(msg_id, session)
    if name in ("macp_job_status", "macp_job_wait", "macp_job_cancel"):
        return await _job(msg_id, name, arguments, session)
    if name in READ_TOOLS:
        return await _read_call(msg_id, name, arguments, session)
    if name in WRITE_TOOLS:
        if not _allows(session.scopes, "mcp:write"):
            return rpc_ok(msg_id, tool_result(
                {"code": "forbidden", "message": "token lacks mcp:write"}, is_error=True))
        if not session.bound:
            return rpc_ok(msg_id, tool_result(
                {"code": "task_not_bound",
                 "message": "call macp_task_next first; write tools act on the bound attempt"},
                is_error=True))
        return await _write_call(msg_id, name, arguments, session)
    return rpc_err(msg_id, -32602, f"unknown tool: {name}")


async def _read_call(msg_id: Any, name: str, arguments: dict, session: Session) -> Response:
    tool, needs_task = READ_TOOLS[name]
    task = str(arguments.pop("task", "") or session.task or "").strip()
    if needs_task and not task:
        return rpc_ok(msg_id, tool_result(
            {"code": "task_required",
             "message": "pass task=<code> or bind one with macp_task_next"},
            is_error=True))
    attempt = int(arguments.pop("attempt", session.attempt) or 1)
    if name == "macp_task_get":
        status, payload = await api_call("GET", f"/v1/tasks/{task}", session.token,
                                         project=session.project)
        if status >= 400:
            return rpc_ok(msg_id, tool_result(
                {"code": "task_not_found" if status == 404 else "task_read_failed",
                 "detail": payload}, is_error=True))
        return rpc_ok(msg_id, tool_result(payload))

    body = {
        "tool_call_id": f"mcp-{secrets.token_hex(6)}",
        "agent": session.agent,
        "task": task,
        "attempt": attempt,
        "arguments": arguments,
    }
    status, payload = await api_call(
        "POST", f"/v1/tools/{tool}/invoke", session.token, body, session.project
    )
    return _forward_result(msg_id, status, payload)


async def _write_call(msg_id: Any, name: str, arguments: dict, session: Session) -> Response:
    tool = WRITE_TOOLS[name]
    if name in ASYNC_TOOLS:
        return await _start_job(msg_id, name, tool, arguments, session)
    status, payload = await api_call(
        "POST", f"/v1/tools/{tool}/invoke", session.task_token or session.token,
        {"tool_call_id": f"mcp-{secrets.token_hex(6)}", "agent": session.agent,
         "task": session.task, "attempt": session.attempt, "arguments": arguments},
        session.project, timeout=300,
    )
    result = _forward_result(msg_id, status, payload)
    if name in TERMINAL_TOOLS and status < 400 and not _is_error_payload(payload):
        session.task = None
        session.task_token = None
        session.attempt = 1
    return result


def _is_error_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("status") == "error"


def _forward_result(msg_id: Any, status: int, payload: Any) -> JSONResponse:
    if status >= 400:
        code = "tool_failed"
        detail: Any = payload
        if isinstance(payload, dict):
            raw = payload.get("detail")
            if isinstance(raw, dict) and raw.get("code"):
                code = raw["code"]
                detail = raw
        return rpc_ok(msg_id, tool_result(
            {"code": code, "message": "tool invocation failed", "detail": detail},
            is_error=True))
    if _is_error_payload(payload):
        return rpc_ok(msg_id, tool_result(payload, is_error=True))
    return rpc_ok(msg_id, tool_result((payload or {}).get("output", payload)))


async def _task_next(msg_id: Any, session: Session) -> Response:
    if not session.agent:
        return rpc_ok(msg_id, tool_result(
            {"code": "agent_required", "message": "token is not bound to an agent"},
            is_error=True))
    if not _allows(session.scopes, "mcp:write"):
        return rpc_ok(msg_id, tool_result(
            {"code": "forbidden", "message": "token lacks mcp:write"}, is_error=True))
    if session.bound:
        return rpc_ok(msg_id, tool_result(
            {"code": "already_bound", "task": session.task,
             "message": "finish or block the current task first"}, is_error=True))

    status, offer = await api_call(
        "GET", f"/v1/assignments/next?agent={session.agent}", session.token,
        project=session.project)
    if status == 204 or not offer:
        return rpc_ok(msg_id, tool_result(
            {"code": "no_assignment", "message": "no work available"}, is_error=True))
    if status >= 400:
        return rpc_ok(msg_id, tool_result(
            {"code": "offer_failed", "detail": offer}, is_error=True))

    assignment_id = (offer or {}).get("assignment_id")
    status, claim = await api_call(
        "POST", f"/v1/assignments/{assignment_id}/claim", session.token,
        project=session.project)
    if status >= 400 or not isinstance(claim, dict):
        return rpc_ok(msg_id, tool_result(
            {"code": "claim_failed", "detail": claim}, is_error=True))

    token = (claim.get("token") or {}).get("value")
    if not token:
        return rpc_ok(msg_id, tool_result(
            {"code": "claim_failed", "message": "no task token in the claim response"},
            is_error=True))
    session.task = str(claim.get("task") or "")
    session.attempt = int(claim.get("attempt") or 1)
    session.task_token = token

    status, work_order = await api_call(
        "POST", "/v1/tools/macp_task_get_work_order/invoke", session.task_token,
        {"tool_call_id": f"mcp-{secrets.token_hex(6)}", "agent": session.agent,
         "task": session.task, "attempt": session.attempt, "arguments": {}},
        session.project)
    return rpc_ok(msg_id, tool_result({
        "task": session.task,
        "attempt": session.attempt,
        "workspace": claim.get("workspace"),
        "leases": claim.get("leases") or [],
        "work_order": ((work_order or {}).get("output", work_order)
                       if status < 400 else {"error": work_order}),
    }))


# --------------------------------------------------------------------- async jobs
async def _start_job(msg_id: Any, name: str, tool: str, arguments: dict,
                     session: Session) -> Response:
    job = Job(id="job-" + secrets.token_urlsafe(10), session=session.id,
              tool=name, task=session.task)
    JOBS[job.id] = job
    body = {
        "tool_call_id": f"mcp-{secrets.token_hex(6)}",
        "agent": session.agent,
        "task": session.task,
        "attempt": session.attempt,
        "arguments": arguments,
    }

    async def run() -> None:
        try:
            status, payload = await api_call(
                "POST", f"/v1/tools/{tool}/invoke", session.task_token or session.token,
                body, session.project, timeout=1800)
            if status >= 400 or _is_error_payload(payload):
                job.status = "failed"
                job.error = payload
            else:
                job.status = "succeeded"
                job.result = (payload or {}).get("output", payload)
        except Exception as exc:  # noqa: BLE001 - the job must always settle
            job.status = "failed"
            job.error = {"code": "job_crashed", "detail": str(exc)[:300]}
        finally:
            job.finished = time.time()

    asyncio.create_task(run())
    return rpc_ok(msg_id, tool_result({"job_id": job.id, "status": "running",
                                       "tool": name, "task": job.task}))


def _job_payload(job: Job) -> dict:
    payload = {"job_id": job.id, "status": job.status, "tool": job.tool,
               "task": job.task,
               "elapsed_ms": int(((job.finished or time.time()) - job.created) * 1000)}
    if job.result is not None:
        payload["result"] = job.result
    if job.error is not None:
        payload["error"] = job.error
    return payload


async def _job(msg_id: Any, name: str, arguments: dict, session: Session) -> Response:
    job_id = str(arguments.get("job_id") or "")
    job = JOBS.get(job_id)
    if job is None or job.session != session.id:
        return rpc_ok(msg_id, tool_result(
            {"code": "job_not_found", "job_id": job_id}, is_error=True))
    if name == "macp_job_status":
        return rpc_ok(msg_id, tool_result(_job_payload(job)))
    if name == "macp_job_wait":
        limit = min(int(arguments.get("timeout_seconds") or GATE_WAIT_SECONDS), 60)
        deadline = time.time() + max(1, limit)
        while job.status == "running" and time.time() < deadline:
            await asyncio.sleep(0.5)
        payload = _job_payload(job)
        if job.status == "running":
            payload["retry_after_ms"] = 2000
        return rpc_ok(msg_id, tool_result(payload))
    if job.status == "running":
        job.status = "cancelled"
        job.finished = time.time()
    return rpc_ok(msg_id, tool_result(_job_payload(job)))


# --------------------------------------------------------------------- resources
async def _tasks(session: Session, limit: int = 20) -> list[dict]:
    status, payload = await api_call("GET", f"/v1/tasks?limit={limit}", session.token,
                                     project=session.project)
    if status != 200 or not isinstance(payload, list):
        return []
    return payload[:limit]


async def _resources_list(msg_id: Any, session: Session) -> Response:
    resources: list[dict] = []
    for task in await _tasks(session):
        code = task.get("code")
        if not code:
            continue
        resources.append({
            "uri": f"macp://task/{code}",
            "name": f"task {code}",
            "description": f"{task.get('state')} · {task.get('title', '')}"[:200],
            "mimeType": "application/json",
        })
        resources.append({
            "uri": f"macp://workorder/{code}",
            "name": f"work order {code}",
            "description": "Work Order (execution, acceptance, write surface)",
            "mimeType": "application/json",
        })
    return rpc_ok(msg_id, {"resources": resources})


async def _resources_read(msg_id: Any, params: dict, session: Session) -> Response:
    uri = str(params.get("uri") or "")
    prefix, _, rest = uri.partition("://")
    if prefix != "macp":
        return rpc_err(msg_id, -32602, f"unsupported uri: {uri}")
    kind, _, ident = rest.partition("/")
    if not ident:
        return rpc_err(msg_id, -32602, f"unsupported uri: {uri}")

    if kind == "task":
        status, payload = await api_call("GET", f"/v1/tasks/{ident}", session.token,
                                         project=session.project)
    elif kind == "workorder":
        status, payload = await api_call(
            "POST", "/v1/tools/macp_task_get_work_order/invoke",
            session.task_token or session.token,
            {"tool_call_id": "mcp-res", "agent": session.agent, "task": ident,
             "attempt": session.attempt, "arguments": {}}, session.project)
        if status == 200 and isinstance(payload, dict):
            payload = payload.get("output", payload)
    elif kind == "document":
        query = ""
        if "?" in ident:
            ident, _, query = ident.partition("?")
        task = session.task or ""
        for part in query.split("&"):
            if part.startswith("task="):
                task = part.split("=", 1)[1]
        status, payload = await api_call(
            "POST", "/v1/tools/task_document_read/invoke", session.token,
            {"tool_call_id": "mcp-doc", "agent": session.agent, "task": task,
             "attempt": 1, "arguments": {"document_id": ident}}, session.project)
        if status == 200 and isinstance(payload, dict):
            payload = payload.get("output", payload)
    else:
        return rpc_err(msg_id, -32602, f"unsupported uri: {uri}")

    if status >= 400:
        return rpc_ok(msg_id, tool_result(
            {"code": "resource_unavailable", "uri": uri, "detail": payload}, is_error=True))
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return rpc_ok(msg_id, {"contents": [{
        "uri": uri, "mimeType": "application/json", "text": text[:MAX_TOOL_OUTPUT]},
    ]})


# ----------------------------------------------------------------------- endpoint
@app.post("/mcp")
async def mcp_post(request: Request,
                   authorization: str | None = Header(default=None),
                   mcp_session_id: str | None = Header(default=None),
                   mcp_protocol_version: str | None = Header(default=None)) -> Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return rpc_err(None, -32700, "parse error", status=400)
    if not isinstance(payload, dict):
        return rpc_err(None, -32600, "invalid request", status=400)
    msg_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}

    if method == "initialize":
        return await _initialize(msg_id, params, authorization)
    if method in ("notifications/initialized", "notifications/cancelled", "ping"):
        return rpc_ok(msg_id, {}) if msg_id is not None else Response(status_code=202)

    session = SESSIONS.get(mcp_session_id or "")
    if session is None:
        return rpc_err(msg_id, -32002, "session_not_initialized",
                       data={"detail": "call initialize first and send Mcp-Session-Id"},
                       status=400)
    if session.expires_at <= time.time():
        SESSIONS.pop(session.id, None)
        return rpc_err(msg_id, -32002, "session_expired", status=400)
    session.expires_at = time.time() + SESSION_TTL

    now = time.time()
    session.calls = [t for t in session.calls if now - t < 60]
    if len(session.calls) >= REQUESTS_PER_MINUTE:
        return rpc_err(msg_id, -32000, "rate_limited",
                       data={"limit_per_minute": REQUESTS_PER_MINUTE}, status=429)
    session.calls.append(now)

    if method == "tools/list":
        return rpc_ok(msg_id, {"tools": _tool_catalog()})
    if method == "tools/call":
        return await _tools_call(msg_id, params, session)
    if method == "resources/list":
        return await _resources_list(msg_id, session)
    if method == "resources/read":
        return await _resources_read(msg_id, params, session)
    return rpc_err(msg_id, -32601, f"method not found: {method}")


@app.get("/mcp")
async def mcp_get() -> Response:
    return JSONResponse(
        {"jsonrpc": "2.0", "error": {"code": -32601, "message": "SSE is not enabled"}},
        status_code=405,
        headers={"Allow": "POST, DELETE"},
    )


@app.delete("/mcp")
async def mcp_delete(mcp_session_id: str | None = Header(default=None)) -> Response:
    if mcp_session_id and mcp_session_id in SESSIONS:
        SESSIONS.pop(mcp_session_id, None)
    return Response(status_code=204)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "macp-mcp",
        "sessions": len(SESSIONS),
        "jobs": len(JOBS),
        "protocol": PROTOCOL_VERSION,
    }
