"""Admin, lease, approval, alert and audit endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .auth import Principal, hash_token, new_token, principal_of, require_admin, require_write_token
from .db import COORDINATOR_ID, ProjectContext, current_project, emit_event, reset_project, set_project
from . import llm_config
import urllib.error
import urllib.request

router = APIRouter(prefix="/v1", tags=["admin"])


def pool(request: Request):
    return request.app.state.pool


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


# ---------------------------------------------------------------- tokens


@router.get("/admin/tokens")
async def list_tokens(
    request: Request,
    agent: str | None = None,
    active: bool = Query(default=True),
    _: Principal = Depends(require_admin),
):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select at.id, ag.code as agent, at.scopes, at.task_id, t.code as task,
                   at.expires_at, at.revoked_at, at.created_at
            from agent_tokens at
            join agents ag on ag.id = at.agent_id
            left join tasks t on t.id = at.task_id
            where at.revoked_at is null and at.expires_at > now()
            order by at.created_at desc limit 100
            """,
        )
    return [
        {
            "id": str(row["id"]),
            "agent": row["agent"],
            "scopes": row["scopes"],
            "task_id": str(row["task_id"]) if row["task_id"] else None,
            "task": row["task"],
            "expires_at": iso(row["expires_at"]),
            "created_at": iso(row["created_at"]),
        }
        for row in rows
    ]


@router.post("/admin/tokens", status_code=201)
async def create_token(
    body: dict, request: Request, _: Principal = Depends(require_admin)
):
    agent_code = body.get("agent")
    scopes = body.get("scopes") or ["tool:invoke", "receipt:write"]
    ttl = int(body.get("ttl_minutes", 240))
    if not agent_code:
        raise HTTPException(422, "agent is required")
    if ttl < 1 or ttl > 24 * 60:
        raise HTTPException(422, "ttl_minutes must be between 1 and 1440")
    value = new_token()
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    async with pool(request).acquire() as conn:
        agent = await conn.fetchrow(
            "select id, code from agents where project_id=$1 and code=$2",
            current_project().id,
            agent_code,
        )
        if not agent:
            raise HTTPException(404, "agent not found")
        task_id = None
        if body.get("task"):
            task_id = await conn.fetchval(
                "select id from tasks where project_id=$1 and code=$2",
                current_project().id,
                body["task"],
            )
            if not task_id:
                raise HTTPException(404, "task not found")
        token_id = await conn.fetchval(
            """
            insert into agent_tokens (agent_id, token_hash, scopes, task_id, expires_at)
            values ($1,$2,$3::text[],$4,$5) returning id
            """,
            agent["id"],
            hash_token(value),
            scopes,
            task_id,
            expires,
        )
        await emit_event(
            conn,
            "token.issued",
            "agent",
            agent["id"],
            1,
            "agent",
            None,
            agent["code"],
            {"token_id": str(token_id), "scopes": scopes, "expires_at": iso(expires)},
        )
    return {
        "id": str(token_id),
        "agent": agent_code,
        "value": value,
        "scopes": scopes,
        "task": body.get("task"),
        "expires_at": iso(expires),
    }


@router.post("/admin/tokens/{token_id}/revoke")
async def revoke_token(
    token_id: uuid.UUID, request: Request, principal: Principal = Depends(require_admin)
):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            update agent_tokens set revoked_at=now()
            where id=$1 and revoked_at is null
            returning agent_id
            """,
            token_id,
        )
    if not row:
        raise HTTPException(404, "token not found or already revoked")
    async with pool(request).acquire() as conn:
        await emit_event(
            conn,
            "token.revoked",
            "agent",
            row["agent_id"],
            1,
            "human",
            principal.agent_id,
            str(token_id),
            {"token_id": str(token_id)},
        )
    return {"id": str(token_id), "revoked": True}


# ---------------------------------------------------------------- projects


@router.get("/projects")
async def list_projects(request: Request, _: Principal = Depends(require_admin)):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            "select id, code, name, created_at from projects order by code"
        )
    return [dict(row) for row in rows]


@router.post("/projects", status_code=201)
async def create_project(
    body: dict, request: Request, _: Principal = Depends(require_admin)
):
    code = str(body.get("code") or "").strip()
    name = str(body.get("name") or code).strip()
    if not code or not code.replace("-", "").isalnum():
        raise HTTPException(422, "project code must be alphanumeric/-")
    project_id = uuid.uuid4()
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            try:
                await conn.execute(
                    "insert into projects (id, code, name, policy) values ($1,$2,$3,'{}'::jsonb)",
                    project_id,
                    code,
                    name,
                )
            except Exception as exc:
                if "duplicate key" in str(exc):
                    raise HTTPException(409, "project code already exists") from exc
                raise
            agent_specs = [
                ("coordinator-1", "coordinator"),
                ("observer-1", "observer"),
                ("worker-e", "worker"),
                ("worker-f", "worker"),
                ("worker-g", "worker"),
                ("worker-smoke", "worker"),
                ("worker-llm", "worker"),
                ("worker-deepseek", "worker"),
            ]
            for agent_code, role in agent_specs:
                await conn.execute(
                    """
                    insert into agents
                      (id, project_id, code, role, model_policy, capabilities, budget, status)
                    values ($1,$2,$3,$4,'{"preference":"default"}','{}'::text[],'{}'::jsonb,'idle')
                    """,
                    uuid.uuid4(),
                    project_id,
                    agent_code,
                    role,
                )
            workstreams = [
                ("stream-core", "Core", ["src/**", "tests/**", "solution.py"]),
                ("stream-ui", "UI", ["web/**"]),
                ("stream-infra", "Infra", ["infra/**", "deploy/**"]),
            ]
            for stream_code, stream_name, surface in workstreams:
                await conn.execute(
                    """
                    insert into workstreams
                      (id, project_id, code, name, responsibility, write_surface,
                       acceptance_profile, wip_limit, owner_agent_id)
                    values ($1,$2,$3,$4,'',$5::jsonb,'{"levels":["L1"]}'::jsonb,2,null)
                    """,
                    uuid.uuid4(),
                    project_id,
                    stream_code,
                    stream_name,
                    surface,
                )
            await conn.execute(
                """
                insert into hotfile_policies
                  (id, project_id, path_glob, lease_key, strategy, active)
                values ($1,$2,'shared/**','shared/','single_writer',true)
                """,
                uuid.uuid4(),
                project_id,
            )
    token = set_project(ProjectContext(project_id, code))
    try:
        from .tool_registry import ensure_repo

        ensure_repo()
    finally:
        reset_project(token)
    return {"id": str(project_id), "code": code, "name": name}


# ---------------------------------------------------------------- llm providers


@router.get("/admin/llm-providers")
async def list_llm_providers(
    request: Request, _: Principal = Depends(require_admin)
):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select id, agent_code, name, base_url, model,
                   price_input_per_1m, price_output_per_1m,
                   price_cache_hit_per_1m, enabled, updated_at,
                   api_key_ciphertext
            from llm_providers where project_id=$1 order by agent_code
            """,
            current_project().id,
        )
    return [
        {
            "id": str(row["id"]),
            "agent_code": row["agent_code"],
            "name": row["name"],
            "base_url": row["base_url"],
            "model": row["model"],
            "price_input_per_1m": float(row["price_input_per_1m"]),
            "price_output_per_1m": float(row["price_output_per_1m"]),
            "price_cache_hit_per_1m": float(row["price_cache_hit_per_1m"]),
            "enabled": row["enabled"],
            "updated_at": iso(row["updated_at"]),
            **llm_config.mask(row["api_key_ciphertext"]),
        }
        for row in rows
    ]


@router.post("/admin/llm-providers")
async def save_llm_provider(
    body: dict, request: Request, _: Principal = Depends(require_admin)
):
    agent_code = str(body.get("agent_code") or "").strip()
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    model = str(body.get("model") or "").strip()
    if not agent_code or not base_url or not model:
        raise HTTPException(422, "agent_code, base_url and model are required")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "base_url must be http(s)")
    name = str(body.get("name") or agent_code).strip()
    api_key = str(body.get("api_key") or "").strip()
    prices = (
        float(body.get("price_input_per_1m", 0) or 0),
        float(body.get("price_output_per_1m", 0) or 0),
        float(body.get("price_cache_hit_per_1m", 0) or 0),
    )
    async with pool(request).acquire() as conn:
        agent = await conn.fetchval(
            "select 1 from agents where project_id=$1 and code=$2",
            current_project().id,
            agent_code,
        )
        if not agent:
            raise HTTPException(404, "agent not found in this project")
        existing = await conn.fetchrow(
            "select api_key_ciphertext from llm_providers where project_id=$1 and agent_code=$2",
            current_project().id,
            agent_code,
        )
        cipher = llm_config.encrypt(api_key) if api_key else (
            existing["api_key_ciphertext"] if existing else None
        )
        row = await conn.fetchrow(
            """
            insert into llm_providers
              (project_id, agent_code, name, base_url, model, api_key_ciphertext,
               price_input_per_1m, price_output_per_1m, price_cache_hit_per_1m,
               enabled, updated_at)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,true,now())
            on conflict (project_id, agent_code) do update set
              name=excluded.name,
              base_url=excluded.base_url,
              model=excluded.model,
              api_key_ciphertext=coalesce(excluded.api_key_ciphertext, llm_providers.api_key_ciphertext),
              price_input_per_1m=excluded.price_input_per_1m,
              price_output_per_1m=excluded.price_output_per_1m,
              price_cache_hit_per_1m=excluded.price_cache_hit_per_1m,
              enabled=true,
              updated_at=now()
            returning id
            """,
            current_project().id,
            agent_code,
            name,
            base_url,
            model,
            cipher,
            prices[0],
            prices[1],
            prices[2],
        )
    return {"id": str(row["id"]), "saved": True, "api_key_set": bool(cipher)}


@router.post("/admin/llm-providers/{provider_id}/test")
async def test_llm_provider(
    provider_id: uuid.UUID,
    request: Request,
    _: Principal = Depends(require_admin),
):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            "select * from llm_providers where id=$1 and project_id=$2",
            provider_id,
            current_project().id,
        )
    if not row:
        raise HTTPException(404, "provider not found")
    key = llm_config.decrypt(row["api_key_ciphertext"])
    if not key:
        raise HTTPException(409, "api key is not configured")
    url = row["base_url"].rstrip("/") + "/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = [m.get("id") for m in payload.get("data", []) if m.get("id")]
        return {
            "ok": True,
            "status": 200,
            "models": models[:20],
            "model_present": row["model"] in models if models else None,
        }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read().decode("utf-8", "replace")[:300]}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc)[:300]}


@router.delete("/admin/llm-providers/{provider_id}")
async def disable_llm_provider(
    provider_id: uuid.UUID,
    request: Request,
    _: Principal = Depends(require_admin),
):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            "update llm_providers set enabled=false, updated_at=now() where id=$1 and project_id=$2 returning id",
            provider_id,
            current_project().id,
        )
    if not row:
        raise HTTPException(404, "provider not found")
    return {"id": str(provider_id), "enabled": False}


@router.get("/llm/config")
async def llm_config_for_agent(
    request: Request, agent: str, _: Principal = Depends(require_write_token)
):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            select * from llm_providers
            where project_id=$1 and agent_code=$2 and enabled=true
            """,
            current_project().id,
            agent,
        )
    if not row:
        raise HTTPException(404, "no active provider for agent")
    return {
        "agent": agent,
        "base_url": row["base_url"],
        "model": row["model"],
        "api_key": llm_config.decrypt(row["api_key_ciphertext"]),
        "price_input_per_1m": float(row["price_input_per_1m"]),
        "price_output_per_1m": float(row["price_output_per_1m"]),
        "price_cache_hit_per_1m": float(row["price_cache_hit_per_1m"]),
        "updated_at": iso(row["updated_at"]),
    }


# ---------------------------------------------------------------- leases


@router.get("/assignments")
async def list_assignments(
    request: Request,
    state: str = "active",
    expired: bool = False,
    agent: str | None = None,
    _: Principal = Depends(require_admin),
):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select a.id, a.state, a.lease_expires_at, a.heartbeat_at, a.created_at,
                   t.code as task, t.state as task_state, ag.code as agent,
                   (a.lease_expires_at < now()) as expired
            from assignments a
            join tasks t on t.id=a.task_id
            join agents ag on ag.id=a.agent_id
            where t.project_id=$1 and ($2='all' or a.state=$2)
              and ($3=false or (a.lease_expires_at < now()
                   and a.state in ('offered','claimed','active','submitted')))
              and ($4::text is null or ag.code=$4)
            order by
              case when a.state in ('offered','claimed','active','submitted') then 0 else 1 end,
              a.lease_expires_at asc nulls last
            limit 2000
            """,
            current_project().id,
            state,
            expired,
            agent,
        )
    return [dict(row) for row in rows]


@router.get("/leases")
async def list_leases(request: Request, state: str = "active"):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select l.id, l.path_glob, l.lease_key, l.mode, l.state, l.fencing_token,
                   l.expires_at, l.renewed_at, l.created_at,
                   t.code as task, ag.code as agent
            from leases l
            join tasks t on t.id = l.owner_task_id
            join agents ag on ag.id = l.owner_agent_id
            where l.project_id=$1 and ($2='all' or l.state=$2)
            order by l.created_at desc limit 200
            """,
            current_project().id,
            state,
        )
    return [dict(row) for row in rows]


@router.post("/leases/{lease_id}/renew")
async def renew_lease(
    lease_id: uuid.UUID,
    body: dict | None = None,
    request: Request = None,
    principal: Principal = Depends(principal_of),
):
    minutes = int((body or {}).get("minutes", 30))
    if minutes < 1 or minutes > 240:
        raise HTTPException(422, "minutes must be between 1 and 240")
    async with pool(request).acquire() as conn:
        lease = await conn.fetchrow(
            "select * from leases where id=$1 and state='active'", lease_id
        )
        if not lease:
            raise HTTPException(404, "active lease not found")
        if principal.kind == "agent" and str(lease["owner_agent_id"]) != principal.agent_id:
            raise HTTPException(403, "lease belongs to another agent")
        row = await conn.fetchrow(
            """
            update leases set expires_at=now()+make_interval(mins => $1), renewed_at=now()
            where id=$2 returning expires_at
            """,
            minutes,
            lease_id,
        )
        await emit_event(
            conn,
            "lease.renewed",
            "lease",
            lease_id,
            1,
            principal.kind,
            principal.agent_id,
            str(lease["owner_task_id"]),
            {"minutes": minutes, "expires_at": iso(row["expires_at"])},
        )
    return {"id": str(lease_id), "expires_at": iso(row["expires_at"])}


@router.post("/leases/{lease_id}/release")
async def release_lease(
    lease_id: uuid.UUID, request: Request, principal: Principal = Depends(principal_of)
):
    async with pool(request).acquire() as conn:
        lease = await conn.fetchrow(
            "select * from leases where id=$1 and state='active'", lease_id
        )
        if not lease:
            raise HTTPException(404, "active lease not found")
        if principal.kind == "agent" and str(lease["owner_agent_id"]) != principal.agent_id:
            raise HTTPException(403, "lease belongs to another agent")
        await conn.execute(
            "update leases set state='released', released_at=now() where id=$1", lease_id
        )
        await emit_event(
            conn,
            "lease.released",
            "lease",
            lease_id,
            1,
            principal.kind,
            principal.agent_id,
            str(lease["owner_task_id"]),
            {"reason": "manual release"},
        )
    return {"id": str(lease_id), "state": "released"}


# ---------------------------------------------------------------- approvals


@router.get("/approvals")
async def list_approvals(request: Request, pending: bool = False):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select ap.id, ap.action, ap.resource, ap.risk, ap.options,
                   ap.default_option, ap.requested_at, ap.expires_at,
                   ap.decision, ap.decided_at, ap.rationale,
                   t.code as task, req.code as requested_by, dec.code as decided_by
            from approvals ap
            left join tasks t on t.id = ap.task_id
            left join agents req on req.id = ap.requested_by
            left join agents dec on dec.id = ap.decided_by
            where ap.project_id=$1 and ($2=false or ap.decision is null)
            order by ap.requested_at desc limit 200
            """,
            current_project().id,
            pending,
        )
    return [dict(row) for row in rows]


@router.post("/approvals", status_code=201)
async def request_approval(
    body: dict, request: Request, _: Principal = Depends(require_admin)
):
    action = body.get("action")
    risk = body.get("risk", "medium")
    if not action:
        raise HTTPException(422, "action is required")
    if risk not in ("low", "medium", "high", "critical"):
        raise HTTPException(422, "risk must be low/medium/high/critical")
    async with pool(request).acquire() as conn:
        task_id = None
        if body.get("task"):
            task_id = await conn.fetchval(
                "select id from tasks where project_id=$1 and code=$2",
                current_project().id,
                body["task"],
            )
            if not task_id:
                raise HTTPException(404, "task not found")
        approval_id = await conn.fetchval(
            """
            insert into approvals (
              project_id, task_id, action, resource, risk, options,
              default_option, requested_by, expires_at
            ) values ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9)
            returning id
            """,
            current_project().id,
            task_id,
            action,
            body.get("resource") or {},
            risk,
            body.get("options") or ["approve", "deny"],
            body.get("default_option") or "deny",
            _agent_id(request),
            datetime.now(timezone.utc) + timedelta(hours=int(body.get("ttl_hours", 24))),
        )
        await emit_event(
            conn,
            "approval.requested",
            "approval",
            approval_id,
            1,
            "agent",
            COORDINATOR_ID,
            str(approval_id),
            {"action": action, "risk": risk, "task": body.get("task")},
        )
    return {"id": str(approval_id), "state": "pending", "risk": risk}


@router.post("/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: uuid.UUID,
    body: dict,
    request: Request,
    force: bool = False,
    principal: Principal = Depends(require_admin),
):
    decision = body.get("decision")
    if decision not in ("approved", "denied", "expired"):
        raise HTTPException(422, "decision must be approved/denied/expired")
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            approval = await conn.fetchrow(
                "select * from approvals where id=$1 and project_id=$2 for update",
                approval_id,
                current_project().id,
            )
            if not approval:
                raise HTTPException(404, "approval not found")
            if approval["decision"]:
                raise HTTPException(409, f"approval already {approval['decision']}")

            # Separation of duties.  With a single shared break-glass token there
            # is only one identity, so a hard ban would make the flow unusable;
            # instead a same-identity decision must be explicit (force=true) and
            # is recorded as a self-decision event.
            decider = _agent_id(request)
            self_decided = approval["requested_by"] and str(
                approval["requested_by"]
            ) == str(decider)
            if self_decided and decision == "approved" and not force:
                raise HTTPException(
                    403,
                    {
                        "code": "self_approval_forbidden",
                        "detail": (
                            "the requester cannot approve their own request; "
                            "use a different identity, or break-glass with ?force=true"
                        ),
                    },
                )
            if self_decided and decision == "approved":
                await emit_event(
                    conn,
                    "approval.self_decided",
                    "approval",
                    approval_id,
                    1,
                    "human",
                    decider,
                    str(approval_id),
                    {"forced": True, "note": "break-glass self approval"},
                )

            await conn.execute(
                """
                update approvals set decision=$1, decided_by=$2, decided_at=now(),
                  rationale=$3 where id=$4
                """,
                decision,
                decider,
                body.get("rationale", ""),
                approval_id,
            )
            await emit_event(
                conn,
                f"approval.{decision}",
                "approval",
                approval_id,
                2,
                "human",
                decider,
                str(approval_id),
                {"decision": decision, "rationale": body.get("rationale", "")},
            )
    return {"id": str(approval_id), "decision": decision, "self_decided": bool(self_decided)}


# ---------------------------------------------------------------- alerts


@router.get("/alerts")
async def list_alerts(request: Request, state: str = "open"):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select id, severity, code, resource, state, first_seen, last_seen,
                   count, details
            from alerts where project_id=$1 and ($2='all' or state=$2)
            order by last_seen desc limit 200
            """,
            current_project().id,
            state,
        )
    return [dict(row) for row in rows]


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(
    alert_id: uuid.UUID,
    body: dict | None = None,
    request: Request = None,
    principal: Principal = Depends(require_admin),
):
    state = (body or {}).get("state", "acked")
    if state not in ("acked", "cleared"):
        raise HTTPException(422, "state must be acked or cleared")
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            "update alerts set state=$1 where id=$2 and project_id=$3 returning id",
            state,
            alert_id,
            current_project().id,
        )
    if not row:
        raise HTTPException(404, "alert not found")
    return {"id": str(alert_id), "state": state}


# ---------------------------------------------------------------- quotas / export


@router.get("/admin/quotas")
async def list_quotas(request: Request, _: Principal = Depends(require_admin)):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            "select * from project_quotas where project_id=$1", current_project().id
        )
    return [dict(row) for row in rows]


@router.post("/admin/quotas")
async def set_quota(
    body: dict, request: Request, _: Principal = Depends(require_admin)
):
    async with pool(request).acquire() as conn:
        await conn.execute(
            """
            insert into project_quotas
              (project_id, max_active_tasks, max_tasks_per_day, max_cost_per_day)
            values ($1,$2,$3,$4)
            on conflict (project_id) do update set
              max_active_tasks=excluded.max_active_tasks,
              max_tasks_per_day=excluded.max_tasks_per_day,
              max_cost_per_day=excluded.max_cost_per_day
            """,
            current_project().id,
            int(body.get("max_active_tasks", 0)),
            int(body.get("max_tasks_per_day", 0)),
            float(body.get("max_cost_per_day", 0)),
        )
    return {"ok": True}


@router.get("/compliance/export")
async def compliance_export(
    request: Request,
    since: int = Query(default=0, ge=0),
    limit_events: int = Query(default=20000, ge=1, le=100000),
    _: Principal = Depends(require_admin),
):
    async def line(payload: dict):
        yield json.dumps(payload, ensure_ascii=False, default=str) + chr(10)

    async def generate():
        async with pool(request).acquire() as conn:
            counts = {
                "tasks": await conn.fetchval(
                    "select count(*)::int from tasks where project_id=$1", current_project().id
                ),
                "receipts": await conn.fetchval(
                    "select count(*)::int from receipts where project_id=$1", current_project().id
                ),
                "reviews": await conn.fetchval(
                    "select count(*)::int from reviews r join receipts x on x.id=r.receipt_id where x.project_id=$1",
                    current_project().id,
                ),
                "events": await conn.fetchval(
                    "select count(*)::int from events where project_id=$1", current_project().id
                ),
            }
            head = await conn.fetchrow(
                "select seq, hash from events where project_id=$1 order by seq desc limit 1",
                current_project().id,
            )
            manifest = {
                "type": "manifest",
                "generated_at": iso(datetime.now(timezone.utc)),
                "project_id": str(current_project().id),
                "counts": counts,
                "event_head": dict(head) if head else None,
                "since": since,
            }
            async for chunk in line(manifest):
                yield chunk
            for row in await conn.fetch(
                "select * from tasks where project_id=$1 order by code", current_project().id
            ):
                async for chunk in line({"type": "task", **dict(row)}):
                    yield chunk
            for row in await conn.fetch(
                "select * from receipts where project_id=$1 order by created_at",
                current_project().id,
            ):
                async for chunk in line({"type": "receipt", **dict(row)}):
                    yield chunk
            for row in await conn.fetch(
                """
                select rv.* from reviews rv
                join receipts rc on rc.id=rv.receipt_id
                where rc.project_id=$1 order by rv.created_at
                """,
                current_project().id,
            ):
                async for chunk in line({"type": "review", **dict(row)}):
                    yield chunk
            for row in await conn.fetch(
                """
                select * from events where project_id=$1 and seq > $2
                order by seq limit $3
                """,
                current_project().id,
                since,
                limit_events,
            ):
                async for chunk in line({"type": "event", **dict(row)}):
                    yield chunk

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=macp-compliance.jsonl"},
    )


# ---------------------------------------------------------------- metrics / audit


@router.get("/metrics/history")
async def metrics_history(request: Request, days: int = Query(default=14, ge=1, le=365)):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select day, metric, value, meta from metrics_daily
            where project_id=$1 and day > current_date - $2::int
            order by day, metric
            """,
            current_project().id,
            days,
        )
    return [dict(row) for row in rows]


@router.get("/audit/chain")
async def audit_chain(request: Request, limit: int = Query(default=200, ge=1, le=5000)):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select seq, id, type, version, aggregate_type, aggregate_id,
                   aggregate_version, actor_type, actor_id, session_id,
                   correlation_id, causation_id, idempotency_key, payload,
                   occurred_at, prev_hash, hash
            from events where project_id=$1 order by seq desc limit $2
            """,
            current_project().id,
            limit,
        )
    rows = list(reversed(rows))
    broken = []
    for index in range(1, len(rows)):
        if rows[index]["prev_hash"] != rows[index - 1]["hash"]:
            broken.append(rows[index]["seq"])
    first = rows[0] if rows else None
    last = rows[-1] if rows else None
    return {
        "checked": len(rows),
        "first_seq": first["seq"] if first else None,
        "last_seq": last["seq"] if last else None,
        "last_hash": last["hash"] if last else None,
        "broken_links": broken,
        "ok": not broken,
    }


@router.get("/audit/anchors")
async def audit_anchors(request: Request, limit: int = Query(default=90, ge=1, le=1000)):
    """Daily chain-head anchors, re-verified against the live events.

    A rewrite of the whole event chain would still pass /audit/chain (it only
    compares adjacent rows).  Anchors pin each day's head hash, and a copy is
    chained into a file on the NAS by deploy/anchor_events.sh, so a silent
    rewrite shows up here as ``live_hash`` differing from the stored ``hash``.
    """

    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select a.day, a.last_seq, a.hash, a.external_ref, a.created_at,
                   e.hash as live_hash
            from audit_anchors a
            left join events e on e.project_id = a.project_id and e.seq = a.last_seq
            where a.project_id = $1
            order by a.day desc
            limit $2
            """,
            current_project().id,
            limit,
        )
    items = []
    broken = []
    for row in rows:
        item = {
            "day": row["day"].isoformat(),
            "last_seq": row["last_seq"],
            "hash": row["hash"],
            "live_hash": row["live_hash"],
            "external_ref": row["external_ref"],
            "created_at": iso(row["created_at"]),
            "ok": row["live_hash"] == row["hash"],
        }
        if not item["ok"]:
            broken.append(item["day"])
        items.append(item)
    return {"anchors": items, "broken": broken, "ok": not broken}


def _agent_id(request: Request):
    principal = getattr(request.state, "principal", None)
    return principal.agent_id if principal and principal.agent_id else COORDINATOR_ID
