"""Authentication and authorization for MACP writes.

Two kinds of bearer credentials are accepted:

* the deployment admin token (``MACP_WRITE_TOKEN`` / mounted secret file), which
  represents a human operator and carries every scope;
* a per-agent token stored as a SHA-256 hash in ``agent_tokens``.  These tokens
  are short lived, can be bound to one task/attempt, and carry explicit scopes.

The admin token remains the break-glass credential.  Agent tokens are what the
runtime and tool gateway use so audit events can be attributed to a real agent
and a stolen worker token cannot touch another task.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .oidc import scopes_for, verify_token

ADMIN_SCOPES = {"*"}

# method, path regex, scope.  The first matching rule wins.
SCOPE_RULES: list[tuple[str, str, str]] = [
    ("POST", r"^/v1/tasks$", "task:create"),
    ("POST", r"^/v1/tasks/[^/]+/dispatch$", "task:dispatch"),
    ("GET", r"^/v1/assignments/next$", "task:claim"),
    ("POST", r"^/v1/assignments/[^/]+/claim$", "task:claim"),
    ("POST", r"^/v1/assignments/[^/]+/heartbeat$", "task:heartbeat"),
    ("POST", r"^/v1/assignments/[^/]+/release$", "task:release"),
    ("POST", r"^/v1/tasks/[^/]+/receipts$", "receipt:write"),
    ("POST", r"^/v1/tasks/[^/]+/reviews$", "review:write"),
    ("POST", r"^/v1/tasks/[^/]+/recycle$", "task:dispatch"),
    ("POST", r"^/v1/tasks/[^/]+/requeue$", "task:dispatch"),
    ("POST", r"^/v1/tasks/[^/]+/rework$", "task:dispatch"),
    ("POST", r"^/v1/tasks/[^/]+/reconcile$", "task:dispatch"),
    ("POST", r"^/v1/agents/[^/]+/events$", "agent:event"),
    ("POST", r"^/v1/tasks/[^/]+/model-usage$", "usage:write"),
    ("POST", r"^/v1/merges$", "merge:write"),
    ("GET", r"^/v1/merges/next$", "merge:write"),
    ("POST", r"^/v1/merges/[^/]+/process$", "merge:write"),
    ("POST", r"^/v1/tools/[^/]+/invoke$", "tool:invoke"),
    ("GET", r"^/v1/tools$", "tool:invoke"),
    ("POST", r"^/v1/leases/[^/]+/renew$", "lease:write"),
    ("POST", r"^/v1/leases/[^/]+/release$", "lease:write"),
    ("GET", r"^/v1/leases$", "lease:read"),
    ("POST", r"^/v1/approvals$", "approval:write"),
    ("POST", r"^/v1/approvals/[^/]+/decide$", "approval:write"),
    ("GET", r"^/v1/approvals$", "approval:read"),
    ("GET", r"^/v1/llm/config$", "llm:config"),
    ("GET", r"^/v1/admin/tokens$", "token:admin"),
    ("POST", r"^/v1/admin/tokens$", "token:admin"),
    ("POST", r"^/v1/admin/tokens/[^/]+/revoke$", "token:admin"),
    ("POST", r"^/v1/admin/observer/run$", "observer:run"),
]


@dataclass
class Principal:
    kind: str  # "admin" | "agent"
    agent_id: str | None = None
    agent_code: str | None = None
    token_id: str | None = None
    scopes: set[str] = field(default_factory=set)
    task_id: str | None = None
    task_code: str | None = None
    attempt_id: str | None = None
    project_id: str | None = None
    project_code: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin" or "*" in self.scopes

    def allows(self, scope: str) -> bool:
        return self.is_admin or scope in self.scopes


def hash_token(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token() -> str:
    return "macp_" + secrets.token_urlsafe(32)


def scope_for(method: str, path: str) -> str | None:
    for rule_method, pattern, scope in SCOPE_RULES:
        if method == rule_method and re.match(pattern, path):
            return scope
    return None


def admin_token(request: Request) -> str:
    return getattr(request.app.state, "write_token", "") or ""


async def _agent_principal(request: Request, supplied: str) -> Principal | None:
    digest = hash_token(supplied)
    conn = request.app.state.pool
    async with conn.acquire() as db:
        row = await db.fetchrow(
            """
            select at.id, at.scopes, at.expires_at, at.revoked_at,
                   at.task_id, at.attempt_id, ag.id as agent_id, ag.code,
                   ag.project_id, p.code as project_code, t.code as task_code
            from agent_tokens at
            join agents ag on ag.id = at.agent_id
            join projects p on p.id = ag.project_id
            left join tasks t on t.id = at.task_id
            where at.token_hash = $1
            """,
            digest,
        )
    if not row:
        return None
    if row["revoked_at"] is not None or row["expires_at"] <= datetime.now(timezone.utc):
        return None
    return Principal(
        kind="agent",
        agent_id=str(row["agent_id"]),
        agent_code=row["code"],
        token_id=str(row["id"]),
        scopes=set(row["scopes"] or []),
        task_id=str(row["task_id"]) if row["task_id"] else None,
        task_code=row["task_code"],
        attempt_id=str(row["attempt_id"]) if row["attempt_id"] else None,
        project_id=str(row["project_id"]),
        project_code=row["project_code"],
    )


async def authenticate(request: Request, authorization: str | None) -> Principal:
    token = admin_token(request)
    if not token and os.getenv("MACP_ALLOW_INSECURE_WRITES") == "1":
        return Principal(kind="admin", scopes=set(ADMIN_SCOPES))
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            401,
            {"code": "auth_required", "detail": "bearer token required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = authorization.removeprefix("Bearer ").strip()
    if not supplied:
        raise HTTPException(401, {"code": "auth_required", "detail": "empty bearer token"})
    if token and secrets.compare_digest(supplied, token):
        return Principal(kind="admin", scopes=set(ADMIN_SCOPES))
    principal = await _agent_principal(request, supplied)
    if principal:
        return principal
    try:
        claims = verify_token(supplied)
        return Principal(
            kind="human",
            agent_code=claims.get("email") or claims.get("sub"),
            scopes=scopes_for(claims),
        )
    except HTTPException:
        pass
    # Constant-time compare against the admin token even on the failure path.
    if token:
        hmac.compare_digest(supplied, token)
    raise HTTPException(401, {"code": "invalid_token", "detail": "invalid or expired token"})


async def require_write_token(
    request: Request, authorization: str | None = Header(default=None)
) -> Principal:
    principal = await authenticate(request, authorization)
    request.state.principal = principal
    if principal.project_id and principal.project_code:
        from .db import ProjectContext, set_project

        request.state.project_token = set_project(
            ProjectContext(__import__("uuid").UUID(principal.project_id), principal.project_code)
        )
    scope = scope_for(request.method, request.url.path)
    if scope and not principal.allows(scope):
        raise HTTPException(
            403,
            {
                "code": "scope_denied",
                "detail": f"token {principal.agent_code or 'agent'} lacks scope {scope}",
                "required_scope": scope,
            },
        )
    return principal


def require_scope(scope: str):
    async def dependency(
        request: Request, authorization: str | None = Header(default=None)
    ) -> Principal:
        current = await require_write_token(request, authorization)
        if not current.allows(scope):
            raise HTTPException(
                403,
                {
                    "code": "scope_denied",
                    "detail": f"token lacks scope {scope}",
                    "required_scope": scope,
                },
            )
        return current

    return dependency


async def require_admin(
    request: Request, authorization: str | None = Header(default=None)
) -> Principal:
    current = await require_write_token(request, authorization)
    if not current.is_admin:
        raise HTTPException(403, {"code": "admin_required", "detail": "admin token required"})
    return current


def assert_agent_owns(principal: Principal, agent_id, assignment_id: str | None = None) -> None:
    """Reject cross-agent use of an assignment-scoped token."""

    if principal.is_admin:
        return
    if principal.agent_id and str(agent_id) != principal.agent_id:
        raise HTTPException(
            403,
            {
                "code": "agent_mismatch",
                "detail": "token belongs to a different agent",
                "assignment_id": assignment_id,
            },
        )


async def issue_agent_token(
    request: Request,
    agent_id,
    scopes: list[str],
    task_id=None,
    attempt_id=None,
    ttl_minutes: int = 240,
) -> tuple[str, str, datetime]:
    """Mint a task-scoped agent token and return (token_id, value, expires_at)."""

    value = new_token()
    from datetime import timedelta

    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    async with request.app.state.pool.acquire() as db:
        async with db.transaction():
            if task_id is not None:
                await db.execute(
                    """
                    update agent_tokens set revoked_at=now()
                    where agent_id=$1 and task_id=$2 and revoked_at is null
                    """,
                    agent_id,
                    task_id,
                )
            token_id = await db.fetchval(
                """
                insert into agent_tokens (
                  agent_id, token_hash, scopes, task_id, attempt_id, expires_at
                ) values ($1,$2,$3::text[],$4,$5,$6) returning id
                """,
                agent_id,
                hash_token(value),
                scopes,
                task_id,
                attempt_id,
                expires,
            )
    return str(token_id), value, expires


def principal_of(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(401, {"code": "auth_required", "detail": "not authenticated"})
    return principal


# --------------------------------------------------------------------- /v1/auth/me
# Read-only principal introspection for adapters (the MCP gateway validates its
# bearer token here).  Never returns the token itself.
me_router = APIRouter(tags=["auth"])


@me_router.get("/v1/auth/me")
async def whoami(principal: Principal = Depends(require_write_token)) -> dict:
    return {
        "kind": principal.kind,
        "agent": principal.agent_code,
        "project": principal.project_code,
        "scopes": sorted(principal.scopes),
        "task": principal.task_code,
        "attempt_id": principal.attempt_id,
        "token_id": principal.token_id,
    }