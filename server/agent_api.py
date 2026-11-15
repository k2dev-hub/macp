"""Structured agent communication API for M0.

These endpoints are the write side of the coordination protocol:
dispatch -> claim -> heartbeat -> receipt -> review. They are intentionally
small but validate the same state transitions and append the same events.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from .auth import (
    Principal,
    assert_agent_owns,
    issue_agent_token,
    principal_of,
    require_admin,
    require_write_token,
)
from .canonical import canonical_json, sha256_hex
from .db import (
    COORDINATOR_ID,
    WORKER_IDS,
    current_project,
    emit_event,
)
from . import signing
from .workorders import (
    LEVEL_NAMES,
    SEED_APP_DIGEST,
    body_hash as workorder_hash,
    default_work_order,
    effective_level,
    lint_work_order,
    required_gates,
    surface_level,
)


router = APIRouter(prefix="/v1", tags=["agent"], dependencies=[Depends(require_write_token)])


def pool(request: Request):
    return request.app.state.pool


def digest(value) -> str:
    return "sha256:" + sha256_hex(canonical_json(value))


def repo_head() -> str:
    """Resolve the real default-branch head used as the attempt base."""

    try:
        from .tool_registry import ensure_repo, git, repo_path

        ensure_repo()
        return git("rev-parse", "main", cwd=repo_path()).stdout.strip()
    except Exception:
        return "demo-base"


def ref_exists(ref: str) -> bool:
    try:
        from .tool_registry import git, repo_path

        proc = git("show-ref", "--verify", "--quiet", ref, cwd=repo_path(), check=False)
        return proc.returncode == 0
    except Exception:
        return False


def candidate_diff(base: str, ref: str) -> tuple[list[str], int]:
    """Return changed paths and total changed lines for a candidate ref."""

    try:
        from .tool_registry import git, repo_path

        numstat = git("diff", "--numstat", f"{base}..{ref}", cwd=repo_path()).stdout
        paths, lines = [], 0
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            paths.append(path)
            if added.isdigit():
                lines += int(added)
            if deleted.isdigit():
                lines += int(deleted)
        return paths, lines
    except Exception:
        return [], 0


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def set_agent(conn, agent_id, status: str, task_id):
    await conn.execute(
        "update agents set status=$1, current_task_id=$2, heartbeat_at=now() where id=$3",
        status,
        task_id,
        agent_id,
    )


@router.post("/tasks", status_code=201)
async def create_task(
    body: dict, request: Request, principal: Principal = Depends(require_write_token)
):
    code = body.get("code")
    if not code:
        code = f"T-9{str(uuid.uuid4().int)[:3]}"
    workstream = body.get("workstream", "stream-core")
    title = body.get("title", "Manual agent task")
    kind = body.get("kind", "slice")
    priority = body.get("priority", "P2")
    budget = body.get("budget") or {
        "max_tool_calls": 40,
        "max_wall_seconds": 900,
        "max_model_calls": 30,
        "max_tokens": 400000,
        "max_cost": 2.0,
    }
    base_task = {
        "code": code,
        "title": title,
        "workstream": workstream,
        "priority": priority,
        "kind": kind,
    }
    requested_docs = body.get("documents") or []
    document_ids = []
    for value in requested_docs:
        try:
            document_ids.append(uuid.UUID(str(value)))
        except ValueError as exc:
            raise HTTPException(422, f"invalid document id: {value}") from exc
    document_meta = []
    if document_ids:
        async with pool(request).acquire() as conn:
            rows = await conn.fetch(
                """
                select id, filename, char_count, sha256
                from task_documents
                where project_id=$1 and id = any($2::uuid[]) and task_id is null
                """,
                current_project().id,
                document_ids,
            )
        if len(rows) != len(document_ids):
            raise HTTPException(409, "one or more documents are missing or already attached")
        document_meta = [
            {
                "id": str(row["id"]),
                "filename": row["filename"],
                "char_count": row["char_count"],
                "sha256": row["sha256"],
            }
            for row in rows
        ]
    work_order = default_work_order(base_task)
    if document_meta:
        work_order["documents"] = document_meta
    work_order.update(body.get("work_order") or {})
    work_order.update(
        {
            "schema": "macp.workorder/v1",
            "task_id": code,
            "attempt": 1,
            "title": title,
            "workstream": workstream,
            "priority": priority,
            "kind": kind,
        }
    )
    base_commit = await asyncio.to_thread(repo_head)
    work_order["base_commit"] = base_commit

    # Declaring a high-risk write surface is an L3 capability.  Without this an
    # agent-scoped token could widen its own blast radius by writing
    # work_order.write_surface = ["deploy/**"] and then only be stopped by the
    # merge-time approval (which the same token can also grant).
    declared = surface_level(work_order.get("write_surface") or [])
    if declared >= 3 and not principal.allows("task:l3"):
        raise HTTPException(
            403,
            {
                "code": "l3_surface_requires_privilege",
                "detail": (
                    "declaring deploy/, server/auth*, migrations/, .sql, Docker or "
                    "server/db.py in write_surface needs an admin token or the task:l3 scope"
                ),
                "level": LEVEL_NAMES[declared],
            },
        )

    dependency_codes = body.get("dependencies") or work_order.get("dependencies") or []
    dependency_codes = [str(value) for value in dependency_codes]
    if code in dependency_codes:
        raise HTTPException(
            422, {"code": "dependency_invalid", "detail": "a task cannot depend on itself"}
        )
    work_order["dependencies"] = dependency_codes
    try:
        from .anchors import resolve as resolve_anchors
        from .tool_registry import repo_path

        anchors = []
        for anchor in work_order.get("anchors") or []:
            if anchor.get("digest") == SEED_APP_DIGEST:
                anchor = {**anchor, "digest": None}
            anchors.append(anchor)
        work_order["anchors"] = await asyncio.to_thread(
            resolve_anchors, base_commit, anchors, repo_path()
        )
    except Exception as exc:
        raise HTTPException(
            422, {"code": "anchor_mismatch", "errors": [str(exc)]}
        ) from exc
    linter_errors = lint_work_order(work_order)
    if linter_errors:
        raise HTTPException(422, {"code": "linter_failed", "errors": linter_errors})
    wo_id = uuid.uuid4()
    wo_hash = workorder_hash(work_order)
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            ws = await conn.fetchrow(
                "select id from workstreams where project_id=$1 and code=$2",
                current_project().id,
                workstream,
            )
            if not ws:
                raise HTTPException(404, "workstream not found")
            quota = await conn.fetchrow(
                "select * from project_quotas where project_id=$1", current_project().id
            )
            if quota:
                if quota["max_active_tasks"]:
                    active = await conn.fetchval(
                        """
                        select count(*)::int from tasks
                        where project_id=$1 and state in
                          ('ready','assigned','in_progress','review','blocked')
                        """,
                        current_project().id,
                    )
                    if active >= quota["max_active_tasks"]:
                        raise HTTPException(
                            429,
                            {
                                "code": "quota_exceeded",
                                "detail": f"active task quota {quota['max_active_tasks']} reached",
                            },
                        )
                if quota["max_tasks_per_day"]:
                    today = await conn.fetchval(
                        "select count(*)::int from tasks where project_id=$1 and created_at::date=current_date",
                        current_project().id,
                    )
                    if today >= quota["max_tasks_per_day"]:
                        raise HTTPException(
                            429,
                            {
                                "code": "quota_exceeded",
                                "detail": f"daily task quota {quota['max_tasks_per_day']} reached",
                            },
                        )
                if float(quota["max_cost_per_day"] or 0):
                    spent = await conn.fetchval(
                        """
                        select coalesce(sum((payload->>'cost')::numeric),0) from events
                        where project_id=$1 and type='model.usage'
                          and occurred_at::date=current_date
                        """,
                        current_project().id,
                    )
                    if float(spent) >= float(quota["max_cost_per_day"]):
                        raise HTTPException(
                            429,
                            {
                                "code": "quota_exceeded",
                                "detail": f"daily cost quota {quota['max_cost_per_day']} reached",
                            },
                        )
            task_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            try:
                await conn.execute(
                    """
                    insert into tasks (
                      id, project_id, code, workstream_id, title, kind, priority, state,
                      current_attempt, budget, acceptance_profile, completion_contract,
                      created_by, created_at, updated_at, ready_at
                    ) values ($1,$2,$3,$4,$5,$6,$7,'ready',0,'{}'::jsonb,
                              '{"levels":["L1"]}'::jsonb,'{}'::jsonb,$8,$9,$9,$9)
                    """,
                    task_id,
                    current_project().id,
                    code,
                    ws["id"],
                    title,
                    kind,
                    priority,
                    COORDINATOR_ID,
                    now,
                )
            except Exception as exc:
                if "duplicate key" in str(exc):
                    raise HTTPException(409, "task code already exists") from exc
                raise
            await conn.execute(
                "update tasks set budget=$1::jsonb where id=$2", budget, task_id
            )
            await conn.execute(
                """
                insert into work_orders (
                  id, task_id, version, body, body_hash, status, validated_at, created_by
                ) values ($1,$2,1,$3::jsonb,$4,'active',now(),$5)
                """,
                wo_id,
                task_id,
                work_order,
                wo_hash,
                COORDINATOR_ID,
            )
            await conn.execute(
                "update tasks set active_work_order_id=$1 where id=$2", wo_id, task_id
            )
            if dependency_codes:
                dep_rows = await conn.fetch(
                    "select id, code from tasks where project_id=$1 and code = any($2::text[])",
                    current_project().id,
                    dependency_codes,
                )
                found = {row["code"]: row["id"] for row in dep_rows}
                missing = [code_of for code_of in dependency_codes if code_of not in found]
                if missing:
                    raise HTTPException(
                        422, {"code": "dependency_missing", "detail": missing}
                    )
                for dep_code in dependency_codes:
                    await conn.execute(
                        """
                        insert into task_dependencies (task_id, depends_on_task_id, kind)
                        values ($1,$2,'hard')
                        on conflict (task_id, depends_on_task_id) do nothing
                        """,
                        task_id,
                        found[dep_code],
                    )
                await emit_event(
                    conn,
                    "task.dependencies_set",
                    "task",
                    task_id,
                    1,
                    "agent",
                    COORDINATOR_ID,
                    code,
                    {"dependencies": dependency_codes},
                )
            if document_ids:
                await conn.execute(
                    """
                    update task_documents set task_id=$1
                    where project_id=$2 and id = any($3::uuid[]) and task_id is null
                    """,
                    task_id,
                    current_project().id,
                    document_ids,
                )
            await emit_event(
                conn,
                "task.created",
                "task",
                task_id,
                1,
                "agent",
                COORDINATOR_ID,
                code,
                {"title": title, "workstream": workstream, "state": "ready"},
            )
    return {
        "code": code,
        "state": "ready",
        "version": 1,
        "work_order_version": 1,
        "work_order_hash": wo_hash,
    }


@router.post("/tasks/{code}/dispatch", status_code=201)
async def dispatch(code: str, body: dict, request: Request):
    agent_code = body.get("agent")
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                code,
            )
            if not task:
                raise HTTPException(404, "task not found")
            if task["state"] != "ready":
                raise HTTPException(409, f"task is {task['state']}, expected ready")
            unmet = await conn.fetch(
                """
                select d2.code, d2.state
                from task_dependencies td
                join tasks d2 on d2.id = td.depends_on_task_id
                where td.task_id=$1 and d2.state not in ('merged','done')
                order by d2.code
                """,
                task["id"],
            )
            if unmet:
                raise HTTPException(
                    409,
                    {
                        "code": "dependencies_unmet",
                        "detail": [{"code": row["code"], "state": row["state"]} for row in unmet],
                    },
                )
            if agent_code:
                agent = await conn.fetchrow(
                    "select * from agents where project_id=$1 and code=$2 for update",
                    current_project().id,
                    agent_code,
                )
            else:
                agent = await conn.fetchrow(
                    """
                    select * from agents
                    where project_id=$1 and role='worker' and status='idle'
                    order by code limit 1 for update
                    """,
                    current_project().id,
                )
            if not agent:
                raise HTTPException(429, "no eligible worker")
            busy = await conn.fetchval(
                """
                select 1 from assignments
                where agent_id=$1 and state in ('offered','claimed','active','submitted')
                limit 1
                """,
                agent["id"],
            )
            if busy:
                raise HTTPException(409, "agent already has an active assignment")
            assignment_id = uuid.uuid4()
            expires = datetime.now(timezone.utc) + timedelta(minutes=30)
            await conn.execute(
                """
                insert into assignments (
                  id, task_id, attempt_id, agent_id, state, lease_expires_at,
                  heartbeat_at, fencing_token
                ) values ($1,$2,null,$3,'offered',$4,now(),1)
                """,
                assignment_id,
                task["id"],
                agent["id"],
                expires,
            )
            version = await conn.fetchval(
                "update tasks set state='assigned', version=version+1, assigned_at=now(), updated_at=now() where id=$1 returning version",
                task["id"],
            )
            await set_agent(conn, agent["id"], "working", task["id"])
            await emit_event(
                conn, "task.assigned", "task", task["id"], version, "agent", COORDINATOR_ID,
                code, {"agent": agent["code"], "assignment_id": str(assignment_id), "attempt": 1},
            )
            await emit_event(
                conn, "assignment.offered", "assignment", assignment_id, 1, "service", None,
                code, {"agent": agent["code"], "expires_at": iso(expires)},
            )
    return {
        "assignment_id": str(assignment_id),
        "agent": agent_code or agent["code"],
        "expires_at": iso(expires),
        "work_order_version": 1,
    }


@router.post("/assignments/{assignment_id}/release")
async def release_assignment(
    assignment_id: uuid.UUID, request: Request, principal: Principal = Depends(principal_of)
):
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            assignment = await conn.fetchrow(
                """
                select a.*, t.code as task_code, t.state as task_state
                from assignments a join tasks t on t.id=a.task_id
                where a.id=$1 for update
                """,
                assignment_id,
            )
            if not assignment:
                raise HTTPException(404, "assignment not found")
            assert_agent_owns(principal, assignment["agent_id"], str(assignment_id))
            if assignment["state"] in ("closed", "expired", "revoked"):
                return {"assignment_id": str(assignment_id), "state": assignment["state"]}
            await conn.execute(
                "update assignments set state='revoked', closed_at=now() where id=$1",
                assignment_id,
            )
            await conn.execute(
                "update leases set state='released', released_at=now() where owner_task_id=$1 and state='active'",
                assignment["task_id"],
            )
            await set_agent(conn, assignment["agent_id"], "idle", None)
            if assignment["task_state"] in ("assigned", "in_progress"):
                await conn.execute(
                    "update tasks set state='blocked', version=version+1, updated_at=now() where id=$1",
                    assignment["task_id"],
                )
            await emit_event(
                conn, "assignment.revoked", "assignment", assignment_id, 1, "agent",
                COORDINATOR_ID, assignment["task_code"], {"reason": "released"},
            )
    return {"assignment_id": str(assignment_id), "state": "revoked"}


@router.post("/tasks/{code}/rework")
async def rework_task(
    code: str, body: dict, request: Request, principal: Principal = Depends(require_admin)
):
    reason = body.get("reason") or "automatic rework after merge conflict"
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                code,
            )
            if not task:
                raise HTTPException(404, "task not found")
            if task["state"] not in ("blocked", "reopened", "failed", "merged"):
                raise HTTPException(409, f"task is {task['state']}, expected blocked/merged")
            previous_attempt = await conn.fetchrow(
                "select * from attempts where task_id=$1 order by number desc limit 1",
                task["id"],
            )
            last_receipt = await conn.fetchrow(
                "select * from receipts where task_id=$1 order by created_at desc limit 1",
                task["id"],
            )
            agent_id = last_receipt["agent_id"] if last_receipt else None
            if not agent_id:
                agent_id = await conn.fetchval(
                    "select agent_id from assignments where task_id=$1 order by created_at desc limit 1",
                    task["id"],
                )
            if not agent_id:
                raise HTTPException(409, "no agent to rework")
            await conn.execute(
                """
                update assignments set state='closed', closed_at=now()
                where task_id=$1 and state in ('offered','claimed','active','submitted')
                """,
                task["id"],
            )
            await conn.execute(
                "update leases set state='released', released_at=now() where owner_task_id=$1 and state='active'",
                task["id"],
            )
            next_attempt = max(
                task["current_attempt"] or 1,
                previous_attempt["number"] if previous_attempt else 1,
            ) + 1
            base = await asyncio.to_thread(repo_head)
            attempt_id = uuid.uuid4()
            await conn.execute(
                """
                insert into attempts (
                  id, task_id, number, state, parent_attempt_id, base_commit, branch, candidate_ref
                ) values ($1,$2,$3,'active',$4,$5,$6,$7)
                """,
                attempt_id,
                task["id"],
                next_attempt,
                previous_attempt["id"] if previous_attempt else None,
                base,
                f"work/{code}/{next_attempt}",
                f"refs/candidates/{code}/{next_attempt}",
            )
            assignment_id = uuid.uuid4()
            await conn.execute(
                """
                insert into assignments (
                  id, task_id, attempt_id, agent_id, state, lease_expires_at,
                  heartbeat_at, fencing_token
                ) values ($1,$2,$3,$4,'offered',now()+interval '30 minutes',now(),1)
                """,
                assignment_id,
                task["id"],
                attempt_id,
                agent_id,
            )
            version = await conn.fetchval(
                """
                update tasks set state='assigned', current_attempt=$1,
                  version=version+1, updated_at=now()
                where id=$2 returning version
                """,
                next_attempt,
                task["id"],
            )
            await set_agent(conn, agent_id, "working", task["id"])
            await emit_event(
                conn, "attempt.created", "attempt", attempt_id, 1, "service", None,
                code, {"attempt": next_attempt, "parent": str(previous_attempt["id"]) if previous_attempt else None},
            )
            await emit_event(
                conn, "task.reworked", "task", task["id"], version, "agent", principal.agent_id,
                code, {"reason": reason, "attempt": next_attempt, "agent": str(agent_id)},
            )
            await emit_event(
                conn, "assignment.offered", "assignment", assignment_id, 1, "service", None,
                code, {"agent": str(agent_id), "attempt": next_attempt},
            )
    return {
        "task": code,
        "state": "assigned",
        "attempt": next_attempt,
        "assignment_id": str(assignment_id),
    }


@router.post("/tasks/{code}/reconcile")
async def reconcile_task(
    code: str, request: Request, principal: Principal = Depends(require_admin)
):
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                code,
            )
            if not task:
                raise HTTPException(404, "task not found")
            merged = await conn.fetchrow(
                """
                select merge_commit from merge_requests
                where task_id=$1 and state='merged'
                order by created_at desc limit 1
                """,
                task["id"],
            )
            if not merged:
                raise HTTPException(409, "no merged merge request for task")
            await conn.execute(
                """
                update assignments set state='closed', closed_at=now()
                where task_id=$1 and state in ('offered','claimed','active','submitted')
                """,
                task["id"],
            )
            await conn.execute(
                "update leases set state='released', released_at=now() where owner_task_id=$1 and state='active'",
                task["id"],
            )
            await conn.execute(
                "update agents set status='idle', current_task_id=null where current_task_id=$1",
                task["id"],
            )
            version = await conn.fetchval(
                "update tasks set state='merged', version=version+1, merged_at=now(), updated_at=now() where id=$1 returning version",
                task["id"],
            )
            await emit_event(
                conn, "task.reconciled", "task", task["id"], version, "human",
                principal.agent_id, code, {"merge_commit": merged["merge_commit"]},
            )
    return {"code": code, "state": "merged", "merge_commit": merged["merge_commit"]}


@router.post("/tasks/{code}/requeue")
async def requeue_task(
    code: str,
    request: Request,
    body: dict | None = None,
    _: Principal = Depends(require_admin),
):
    reason = (body or {}).get("reason") or "manual requeue"
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                code,
            )
            if not task:
                raise HTTPException(404, "task not found")
            if task["state"] not in ("blocked", "reopened", "failed"):
                raise HTTPException(409, f"task is {task['state']}, expected blocked/reopened")
            await conn.execute(
                """
                update assignments set state='closed', closed_at=now()
                where task_id=$1 and state in ('offered','claimed','active','submitted')
                """,
                task["id"],
            )
            await conn.execute(
                """
                update leases set state='released', released_at=now()
                where owner_task_id=$1 and state='active'
                """,
                task["id"],
            )
            await conn.execute(
                """
                update agents set status='idle', current_task_id=null
                where current_task_id=$1 and status in ('working','blocked','waiting_user')
                """,
                task["id"],
            )
            version = await conn.fetchval(
                """
                update tasks set state='ready', version=version+1, updated_at=now()
                where id=$1 returning version
                """,
                task["id"],
            )
            await emit_event(
                conn, "task.requeued", "task", task["id"], version, "human",
                _.agent_id, code, {"reason": reason},
            )
    return {"code": code, "state": "ready", "version": version}


@router.post("/assignments/{assignment_id}/expire")
async def expire_assignment(
    assignment_id: uuid.UUID,
    request: Request,
    _: Principal = Depends(require_admin),
):
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            assignment = await conn.fetchrow(
                """
                select a.*, t.code as task_code, t.state as task_state
                from assignments a join tasks t on t.id=a.task_id
                where a.id=$1 for update
                """,
                assignment_id,
            )
            if not assignment:
                raise HTTPException(404, "assignment not found")
            if assignment["state"] in ("closed", "expired", "revoked"):
                return {"assignment_id": str(assignment_id), "state": assignment["state"]}
            await conn.execute(
                "update assignments set state='expired', closed_at=now() where id=$1",
                assignment_id,
            )
            await conn.execute(
                "update leases set state='released', released_at=now() where owner_task_id=$1 and state='active'",
                assignment["task_id"],
            )
            await set_agent(conn, assignment["agent_id"], "idle", None)
            if assignment["task_state"] in ("assigned", "in_progress"):
                version = await conn.fetchval(
                    """
                    update tasks set state='ready', version=version+1, updated_at=now()
                    where id=$1 returning version
                    """,
                    assignment["task_id"],
                )
                await emit_event(
                    conn, "task.requeued", "task", assignment["task_id"], version,
                    "service", None, assignment["task_code"],
                    {"reason": "assignment lease expired"},
                )
            await emit_event(
                conn, "assignment.expired", "assignment", assignment_id, 1,
                "service", None, assignment["task_code"],
                {"agent": str(assignment["agent_id"])},
            )
    return {"assignment_id": str(assignment_id), "state": "expired"}


@router.get("/assignments/next")
async def next_assignment(request: Request, agent: str | None = None):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            select a.id as assignment_id, a.lease_expires_at, t.code as task, t.title,
                   ag.code as agent
            from assignments a
            join tasks t on t.id=a.task_id
            join agents ag on ag.id=a.agent_id
            where t.project_id=$1 and a.state='offered' and t.state in ('assigned','in_progress')
              and ($2::text is null or ag.code=$2)
            order by a.created_at limit 1
            """,
            current_project().id,
            agent,
        )
    if not row:
        return Response(status_code=204)
    return {
        "assignment_id": str(row["assignment_id"]),
        "task": row["task"],
        "title": row["title"],
        "agent": row["agent"],
        "expires_at": row["lease_expires_at"].isoformat(),
    }


@router.post("/assignments/{assignment_id}/claim")
async def claim(
    assignment_id: uuid.UUID, request: Request, principal: Principal = Depends(principal_of)
):
    base_commit = await asyncio.to_thread(repo_head)
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            assignment = await conn.fetchrow(
                """
                select a.*, t.code as task_code, t.title, t.current_attempt,
                       ag.code as agent_code
                from assignments a
                join tasks t on t.id=a.task_id
                join agents ag on ag.id=a.agent_id
                where a.id=$1 for update
                """,
                assignment_id,
            )
            if not assignment:
                raise HTTPException(404, "assignment not found")
            assert_agent_owns(principal, assignment["agent_id"], str(assignment_id))
            if assignment["state"] != "offered":
                raise HTTPException(409, f"assignment is {assignment['state']}")
            attempt_number = max(1, assignment["current_attempt"] or 0)
            attempt = await conn.fetchrow(
                "select * from attempts where task_id=$1 and number=$2",
                assignment["task_id"],
                attempt_number,
            )
            if not attempt:
                attempt_id = uuid.uuid4()
                await conn.execute(
                    """
                    insert into attempts (id, task_id, number, state, base_commit, branch, candidate_ref)
                    values ($1,$2,$3,'active',$4,$5,$6)
                    """,
                    attempt_id,
                    assignment["task_id"],
                    attempt_number,
                    base_commit,
                    f"work/{assignment['task_code']}/{attempt_number}",
                    f"refs/candidates/{assignment['task_code']}/{attempt_number}",
                )
            else:
                attempt_id = attempt["id"]
                base_commit = attempt["base_commit"] or base_commit
            await conn.execute(
                """
                update assignments
                set state='active', attempt_id=$1, claimed_at=now(), heartbeat_at=now(),
                    lease_expires_at=now()+interval '2 minutes'
                where id=$2
                """,
                attempt_id,
                assignment_id,
            )
            version = await conn.fetchval(
                "update tasks set state='in_progress', current_attempt=$1, version=version+1, updated_at=now() where id=$2 returning version",
                attempt_number,
                assignment["task_id"],
            )
            await set_agent(conn, assignment["agent_id"], "working", assignment["task_id"])
            await emit_event(
                conn, "assignment.claimed", "assignment", assignment_id, 1, "agent",
                assignment["agent_id"], assignment["task_code"], {"attempt": attempt_number},
            )
            await emit_event(
                conn, "task.started", "task", assignment["task_id"], version, "agent",
                assignment["agent_id"], assignment["task_code"], {"attempt": attempt_number},
            )
    from .tool_registry import ensure_worktree

    await asyncio.to_thread(
        ensure_worktree,
        assignment["task_code"],
        attempt_number,
        base_commit,
    )
    token_id, token_value, token_expires = await issue_agent_token(
        request,
        assignment["agent_id"],
        [
            "tool:invoke",
            "repo:read",
            "workspace:read",
            "workspace:write",
            "lease:write",
            "gate:run",
            "receipt:write",
            "review:write",
            "task:heartbeat",
            "task:release",
            "agent:event",
            "usage:write",
            "web:search",
            "doc:read",
        ],
        task_id=assignment["task_id"],
        attempt_id=attempt_id,
        ttl_minutes=240,
    )
    return {
        "task": assignment["task_code"],
        "attempt": attempt_number,
        "workspace": {
            "id": str(assignment["task_id"]),
            "task": assignment["task_code"],
            "attempt": attempt_number,
            "path": f"/work/{assignment['task_code']}/{attempt_number}",
            "branch": f"work/{assignment['task_code']}/{attempt_number}",
            "base_commit": base_commit,
            "candidate_ref": f"refs/candidates/{assignment['task_code']}/{attempt_number}",
            "state": "ready",
        },
        "leases": [],
        "token": {
            "id": token_id,
            "value": token_value,
            "expires_at": iso(token_expires),
            "scopes": [
                "tool:invoke",
                "repo:read",
                "workspace:read",
                "workspace:write",
                "gate:run",
                "receipt:write",
                "review:write",
                "task:heartbeat",
                "task:release",
                "agent:event",
                "usage:write",
                "web:search",
                "doc:read",
            ],
        },
    }


@router.post("/assignments/{assignment_id}/heartbeat")
async def heartbeat(
    assignment_id: uuid.UUID, request: Request, principal: Principal = Depends(principal_of)
):
    async with pool(request).acquire() as conn:
        owner = await conn.fetchval(
            "select agent_id from assignments where id=$1", assignment_id
        )
        if owner:
            assert_agent_owns(principal, owner, str(assignment_id))
        row = await conn.fetchrow(
            """
            update assignments
            set heartbeat_at=now(), lease_expires_at=now()+interval '2 minutes'
            where id=$1 and state in ('claimed','active','submitted')
            returning id, lease_expires_at, agent_id
            """,
            assignment_id,
        )
        if row:
            await conn.execute(
                "update agents set heartbeat_at=now() where id=$1", row["agent_id"]
            )
    if not row:
        raise HTTPException(410, "assignment revoked or expired")
    return {"assignment_id": str(row["id"]), "expires_at": row["lease_expires_at"].isoformat()}


@router.post("/tasks/{code}/receipts", status_code=201)
async def submit_receipt(
    code: str, body: dict, request: Request, principal: Principal = Depends(principal_of)
):
    if principal.task_code and principal.task_code != code:
        raise HTTPException(
            403,
            {
                "code": "task_mismatch",
                "detail": f"token is bound to {principal.task_code}, not {code}",
            },
        )
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                code,
            )
            if not task:
                raise HTTPException(404, "task not found")
            if task["state"] not in ("in_progress", "assigned"):
                raise HTTPException(409, f"task is {task['state']}, expected in_progress")
            assignment = await conn.fetchrow(
                """
                select * from assignments
                where task_id=$1 and state in ('active','claimed')
                order by created_at desc limit 1 for update
                """,
                task["id"],
            )
            if not assignment:
                raise HTTPException(409, "no active assignment")
            assert_agent_owns(principal, assignment["agent_id"], str(assignment["id"]))
            attempt = await conn.fetchrow(
                "select * from attempts where task_id=$1 order by number desc limit 1",
                task["id"],
            )
            attempt_number = (
                attempt["number"] if attempt else max(1, task["current_attempt"] or 1)
            )
            if attempt:
                attempt_id = attempt["id"]
            else:
                attempt_id = uuid.uuid4()
                await conn.execute(
                    """
                    insert into attempts (id, task_id, number, state, base_commit, branch, candidate_ref)
                    values ($1,$2,$3,'active','demo-base',$4,$5)
                    """,
                    attempt_id,
                    task["id"],
                    attempt_number,
                    f"work/{code}/{attempt_number}",
                    f"refs/candidates/{code}/{attempt_number}",
                )
            receipt_id = body.get("receipt_id") or f"R-{uuid.uuid4().int % 10**9:09d}"
            receipt_uuid = uuid.uuid4()
            candidate_ref = body.get(
                "candidate_ref", f"refs/candidates/{code}/{attempt_number}"
            )
            base_commit = (
                body.get("base_commit")
                or (attempt["base_commit"] if attempt else None)
                or "demo-base"
            )
            agent_id = assignment["agent_id"]

            work_order_row = None
            if task["active_work_order_id"]:
                work_order_row = await conn.fetchrow(
                    "select body, body_hash from work_orders where id=$1",
                    task["active_work_order_id"],
                )
            work_order = work_order_row["body"] if work_order_row else {}
            gates = body.get("gates") or []
            if not gates:
                raise HTTPException(
                    422, {"code": "missing_gates", "errors": ["receipt requires gate evidence"]}
                )
            present = {gate.get("gate"): gate for gate in gates}
            changed_paths, changed_lines = (
                await asyncio.to_thread(candidate_diff, base_commit, candidate_ref)
                if candidate_ref.startswith("refs/") and ref_exists(candidate_ref)
                else ([], 0)
            )
            level = effective_level(work_order, changed_paths, changed_lines)
            required = required_gates(work_order, changed_paths, changed_lines)
            missing = [
                gate
                for gate in required
                if gate not in present or int(present[gate].get("exit", 1)) != 0
            ]
            if missing:
                raise HTTPException(
                    422,
                    {
                        "code": "missing_gates",
                        "errors": [f"missing or failing gate: {gate}" for gate in missing],
                    },
                )
            for gate in gates:
                evidence_id = gate.get("evidence_id")
                if not evidence_id:
                    raise HTTPException(
                        422, {"code": "missing_evidence", "errors": [f"gate {gate.get('gate')} has no evidence"]}
                    )
                try:
                    evidence_uuid = uuid.UUID(str(evidence_id))
                except ValueError as exc:
                    raise HTTPException(
                        422, {"code": "missing_evidence", "errors": [str(evidence_id)]}
                    ) from exc
                exists = await conn.fetchval(
                    "select 1 from evidence where id=$1 and project_id=$2",
                    evidence_uuid,
                    current_project().id,
                )
                if not exists:
                    raise HTTPException(
                        422,
                        {
                            "code": "missing_evidence",
                            "errors": [f"unknown evidence_id {evidence_id}"],
                        },
                    )
            if candidate_ref.startswith("refs/") and not ref_exists(candidate_ref):
                raise HTTPException(
                    422,
                    {
                        "code": "unknown_candidate",
                        "errors": [f"candidate ref does not exist: {candidate_ref}"],
                    },
                )
            current_head = await asyncio.to_thread(repo_head)
            stale_base = bool(current_head and base_commit and base_commit != current_head)

            candidate_commit = None
            if candidate_ref.startswith("refs/"):
                try:
                    from .tool_registry import git, repo_path

                    candidate_commit = git(
                        "rev-parse", "--verify", candidate_ref, cwd=repo_path(), check=False
                    ).stdout.strip() or None
                except Exception:
                    candidate_commit = None
            receipt_body = {
                **body,
                "schema": "macp.receipt/v1",
                "receipt_id": receipt_id,
                "task_id": code,
                "attempt": attempt_number,
                "agent_id": str(agent_id),
                "candidate_commit": candidate_commit,
                "risk_level": LEVEL_NAMES.get(level, "L1"),
                "changed_paths": changed_paths,
                "changed_lines": changed_lines,
            }
            work_order_hash = (
                work_order_row["body_hash"] if work_order_row else digest({"task": code})
            )
            body_hash = digest(receipt_body)
            agent_code = await conn.fetchval(
                "select code from agents where id=$1", agent_id
            )
            try:
                signature = signing.sign(agent_code, body_hash)
            except signing.SigningUnavailable as exc:
                # Fail closed: a receipt without a real Ed25519 signature used to
                # be stored with a hash placeholder, which made verification fail
                # silently while the receipt was still accepted.
                raise HTTPException(
                    503,
                    {"code": "signing_unavailable", "detail": str(exc)[:200]},
                ) from exc
            try:
                await conn.execute(
                    """
                    insert into receipts (
                      id, project_id, code, task_id, attempt_id, agent_id, session_id,
                      work_order_hash, base_commit, candidate_ref, body, body_hash,
                      status, stale_base, signature
                    ) values ($1,$2,$3,$4,$5,$6,null,$7,$8,$9,$10::jsonb,$11,
                              'submitted',$13,$12)
                    """,
                    receipt_uuid,
                    current_project().id,
                    receipt_id,
                    task["id"],
                    attempt_id,
                    agent_id,
                    work_order_hash,
                    base_commit,
                    candidate_ref,
                    receipt_body,
                    body_hash,
                    signature,
                    stale_base,
                )
            except Exception as exc:
                if "duplicate key" in str(exc):
                    raise HTTPException(409, "receipt already exists") from exc
                raise
            version = await conn.fetchval(
                "update tasks set state='review', version=version+1, updated_at=now() where id=$1 returning version",
                task["id"],
            )
            await conn.execute(
                "update assignments set state='submitted', heartbeat_at=now() where id=$1",
                assignment["id"],
            )
            await set_agent(conn, agent_id, "waiting_user", task["id"])
            await emit_event(
                conn, "receipt.submitted", "receipt", receipt_uuid, 1, "agent", agent_id,
                code, {"receipt_id": receipt_id, "attempt": attempt_number},
            )
            await emit_event(
                conn, "task.completed", "task", task["id"], version, "agent", agent_id,
                code, {"receipt_id": receipt_id, "attempt": attempt_number},
            )
    return {
        "receipt_id": receipt_id,
        "task_state": "review",
        "required_gates": required,
        "missing_evidence": [],
        "stale_base": stale_base,
    }


@router.post("/tasks/{code}/reviews")
async def review(
    code: str, body: dict, request: Request, principal: Principal = Depends(principal_of)
):
    receipt_code = body.get("receipt_id")
    decision = body.get("decision")
    findings = body.get("findings", [])
    requested_reviewer = body.get("reviewer")
    if principal.kind == "agent":
        if requested_reviewer and requested_reviewer != principal.agent_code:
            raise HTTPException(403, "agent token cannot review as another agent")
        reviewer_code = principal.agent_code
    else:
        reviewer_code = requested_reviewer or "coordinator-1"
    if decision not in ("accept", "reject"):
        raise HTTPException(422, "decision must be accept or reject")
    if decision == "reject" and not findings:
        raise HTTPException(422, "reject requires findings")
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                code,
            )
            if not task:
                raise HTTPException(404, "task not found")
            if task["state"] != "review":
                raise HTTPException(409, f"task is {task['state']}, expected review")
            receipt = await conn.fetchrow(
                "select * from receipts where project_id=$1 and code=$2 for update",
                current_project().id,
                receipt_code,
            )
            if not receipt:
                raise HTTPException(404, "receipt not found")
            reviewer = await conn.fetchrow(
                "select * from agents where project_id=$1 and code=$2",
                current_project().id,
                reviewer_code,
            )
            if not reviewer:
                raise HTTPException(404, "reviewer not found")
            if receipt["agent_id"] == reviewer["id"]:
                raise HTTPException(403, "reviewer must differ from author")
            if decision == "accept":
                if not task["active_work_order_id"]:
                    raise HTTPException(
                        409,
                        {"code": "no_work_order", "detail": "task has no active work order"},
                    )
                gates = (receipt["body"] or {}).get("gates") or []
                if not gates:
                    raise HTTPException(
                        409,
                        {"code": "missing_evidence", "detail": "receipt has no gate evidence"},
                    )
                present = {gate.get("gate"): gate for gate in gates}
                work_order_row = None
                if task["active_work_order_id"]:
                    work_order_row = await conn.fetchrow(
                        "select body from work_orders where id=$1",
                        task["active_work_order_id"],
                    )
                required = required_gates(
                    work_order_row["body"] if work_order_row else {},
                    (receipt["body"] or {}).get("changed_paths") or [],
                    int((receipt["body"] or {}).get("changed_lines") or 0),
                )
                failing = [
                    gate
                    for gate in required
                    if gate not in present or int(present[gate].get("exit", 1)) != 0
                ]
                if failing:
                    raise HTTPException(
                        409,
                        {
                            "code": "missing_gates",
                            "detail": "receipt lacks required passing gates",
                            "gates": failing,
                        },
                    )
                for gate in gates:
                    evidence_id = gate.get("evidence_id")
                    try:
                        evidence_uuid = uuid.UUID(str(evidence_id))
                    except (ValueError, TypeError) as exc:
                        raise HTTPException(
                            409, {"code": "missing_evidence", "detail": str(evidence_id)}
                        ) from exc
                    exists = await conn.fetchval(
                        "select 1 from evidence where id=$1 and project_id=$2",
                        evidence_uuid,
                        current_project().id,
                    )
                    if not exists:
                        raise HTTPException(
                            409,
                            {"code": "missing_evidence", "detail": str(evidence_id)},
                        )
            review_id = uuid.uuid4()
            evidence_set_hash = digest({"receipt": receipt_code, "gates": receipt["body"].get("gates", [])})
            await conn.execute(
                """
                insert into reviews (
                  id, receipt_id, reviewer_agent_id, decision, findings,
                  evidence_set_hash, signature
                ) values ($1,$2,$3,$4,$5::jsonb,$6,'demo-reviewer')
                """,
                review_id,
                receipt["id"],
                reviewer["id"],
                decision,
                findings,
                evidence_set_hash,
            )
            await conn.execute(
                "update receipts set status=$1 where id=$2",
                "accepted" if decision == "accept" else "rejected",
                receipt["id"],
            )
            assignment = await conn.fetchrow(
                "select * from assignments where task_id=$1 order by created_at desc limit 1",
                task["id"],
            )
            if decision == "accept":
                version = await conn.fetchval(
                    "update tasks set state='accepted', version=version+1, accepted_at=now(), updated_at=now() where id=$1 returning version",
                    task["id"],
                )
                if assignment:
                    await conn.execute(
                        "update assignments set state='closed', closed_at=now() where id=$1",
                        assignment["id"],
                    )
                await set_agent(conn, receipt["agent_id"], "idle", None)
                await emit_event(
                    conn, "review.accepted", "review", review_id, 1, "agent", reviewer["id"],
                    code, {"receipt_id": receipt_code, "reviewer": reviewer_code},
                )
                await emit_event(
                    conn, "task.accepted", "task", task["id"], version, "agent", reviewer["id"],
                    code, {"review_id": str(review_id)},
                )
                state = "accepted"
            else:
                previous_attempt = await conn.fetchrow(
                    "select * from attempts where task_id=$1 order by number desc limit 1",
                    task["id"],
                )
                next_attempt = max(task["current_attempt"] or 1, previous_attempt["number"] if previous_attempt else 1) + 1
                if assignment:
                    await conn.execute(
                        "update assignments set state='closed', closed_at=now() where id=$1",
                        assignment["id"],
                    )
                    await emit_event(
                        conn, "assignment.closed", "assignment", assignment["id"], 1, "agent",
                        reviewer["id"], code, {"reason": "review rejected"},
                    )
                version = await conn.fetchval(
                    "update tasks set state='assigned', current_attempt=$1, version=version+1, assigned_at=now(), updated_at=now() where id=$2 returning version",
                    next_attempt,
                    task["id"],
                )
                new_attempt_id = uuid.uuid4()
                await conn.execute(
                    """
                    insert into attempts (
                      id, task_id, number, state, parent_attempt_id, base_commit, branch, candidate_ref
                    ) values ($1,$2,$3,'active',$4,$5,$6,$7)
                    """,
                    new_attempt_id,
                    task["id"],
                    next_attempt,
                    previous_attempt["id"] if previous_attempt else None,
                    previous_attempt["base_commit"] if previous_attempt else "main",
                    f"work/{code}/{next_attempt}",
                    f"refs/candidates/{code}/{next_attempt}",
                )
                new_assignment_id = uuid.uuid4()
                await conn.execute(
                    """
                    insert into assignments (
                      id, task_id, attempt_id, agent_id, state, lease_expires_at,
                      heartbeat_at, fencing_token
                    ) values ($1,$2,$3,$4,'offered',now()+interval '30 minutes',now(),1)
                    """,
                    new_assignment_id,
                    task["id"],
                    new_attempt_id,
                    receipt["agent_id"],
                )
                await set_agent(conn, receipt["agent_id"], "working", task["id"])
                await emit_event(
                    conn, "attempt.created", "attempt", new_attempt_id, 1, "service", None,
                    code, {"attempt": next_attempt, "parent": str(previous_attempt["id"]) if previous_attempt else None},
                )
                await emit_event(
                    conn, "assignment.offered", "assignment", new_assignment_id, 1, "service", None,
                    code, {"agent": str(receipt["agent_id"]), "attempt": next_attempt},
                )
                await emit_event(
                    conn, "review.rejected", "review", review_id, 1, "agent", reviewer["id"],
                    code, {"receipt_id": receipt_code, "findings": findings},
                )
                await emit_event(
                    conn, "task.rejected", "task", task["id"], version, "agent", reviewer["id"],
                    code, {"reason": "review rejected", "attempt": next_attempt},
                )
                state = "assigned"
    return {
        "task": code,
        "state": state,
        "next_attempt": next_attempt if state == "assigned" else None,
        "assignment_id": str(new_assignment_id) if state == "assigned" else None,
    }


@router.post("/tasks/{code}/recycle")
async def recycle_task(code: str, request: Request):
    if not code.startswith("T-8"):
        raise HTTPException(400, "only demo T-8 tasks can be recycled")
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                code,
            )
            if not task:
                raise HTTPException(404, "task not found")
            if task["state"] != "accepted":
                raise HTTPException(409, f"task is {task['state']}, expected accepted")
            next_attempt = task["current_attempt"] + 1
            version = await conn.fetchval(
                """
                update tasks
                set state='ready', accepted_at=null, assigned_at=null,
                    current_attempt=current_attempt+1, updated_at=now(),
                    version=version+1
                where id=$1 returning version
                """,
                task["id"],
            )
            await emit_event(
                conn, "demo.task_recycled", "task", task["id"], version, "service", None,
                code, {"reason": "demo pool recycle", "max_tasks": 30, "attempt": next_attempt},
            )
    return {"code": code, "state": "ready", "version": version, "attempt": next_attempt}


@router.post("/agents/{agent_code}/events", status_code=202)
async def agent_event(
    agent_code: str,
    body: dict,
    request: Request,
    principal: Principal = Depends(principal_of),
):
    if principal.kind == "agent" and principal.agent_code != agent_code:
        raise HTTPException(403, "agent token cannot emit events for another agent")
    event_type = body.get("type")
    payload = body.get("payload", {})
    async with pool(request).acquire() as conn:
        agent = await conn.fetchrow(
            "select * from agents where project_id=$1 and code=$2",
            current_project().id,
            agent_code,
        )
        if not agent:
            raise HTTPException(404, "agent not found")
        await conn.execute(
            "update agents set heartbeat_at=now() where id=$1", agent["id"]
        )
        if event_type == "task.completed":
            receipt_code = payload.get("receipt_id")
            if not receipt_code:
                raise HTTPException(422, "task.completed requires receipt_id")
            receipt = await conn.fetchrow(
                "select * from receipts where project_id=$1 and code=$2",
                current_project().id,
                receipt_code,
            )
            if not receipt:
                raise HTTPException(422, "receipt not found")
            await emit_event(
                conn, "task.completed", "task", receipt["task_id"], 1, "agent", agent["id"],
                payload.get("task", agent_code), {"receipt_id": receipt_code},
            )
            return {"accepted": True}
        if event_type == "model.usage":
            task_code = payload.get("task")
            if not task_code:
                raise HTTPException(422, "model.usage requires task")
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                task_code,
            )
            if task:
                price_in = float(os.getenv("LLM_PRICE_INPUT_PER_1M", "0") or 0)
                price_out = float(os.getenv("LLM_PRICE_OUTPUT_PER_1M", "0") or 0)
                price_cache = float(
                    os.getenv("LLM_PRICE_CACHE_HIT_PER_1M", str(price_in)) or 0
                )
                provider = await conn.fetchrow(
                    """
                    select price_input_per_1m, price_output_per_1m, price_cache_hit_per_1m
                    from llm_providers
                    where project_id=$1 and agent_code=$2 and enabled=true
                    """,
                    current_project().id,
                    agent["code"],
                )
                if provider:
                    price_in = float(provider["price_input_per_1m"])
                    price_out = float(provider["price_output_per_1m"])
                    price_cache = float(provider["price_cache_hit_per_1m"])
                prompt = int(payload.get("prompt_tokens", 0) or 0)
                completion = int(payload.get("completion_tokens", 0) or 0)
                cache_hit = int(payload.get("prompt_cache_hit_tokens", 0) or 0)
                cache_hit = max(0, min(cache_hit, prompt))
                if price_in or price_out or price_cache:
                    call_cost = (
                        (prompt - cache_hit) * price_in
                        + cache_hit * price_cache
                        + completion * price_out
                    ) / 1_000_000
                else:
                    call_cost = payload.get("cost", 0)
                payload["cost"] = round(float(call_cost), 9)
                payload["price_input_per_1m"] = price_in
                payload["price_output_per_1m"] = price_out
                payload["price_cache_hit_per_1m"] = price_cache
                payload.setdefault("model_calls", 1)
                await emit_event(
                    conn, "model.usage", "task", task["id"], task["version"], "agent",
                    agent["id"], task_code, payload,
                )
                spent = await conn.fetchval(
                    """
                    select coalesce(sum((payload->>'cost')::numeric),0) from events
                    where project_id=$1 and correlation_id=$2 and type='model.usage'
                    """,
                    current_project().id,
                    task_code,
                )
                budget = task["budget"] or {}
                max_cost = float(budget.get("max_cost", 0) or 0)
                exhausted = bool(max_cost) and float(spent) > max_cost
                if exhausted and task["state"] in ("assigned", "in_progress"):
                    await conn.execute(
                        """
                        update tasks set state='blocked', version=version+1, updated_at=now()
                        where id=$1
                        """,
                        task["id"],
                    )
                    await conn.execute(
                        """
                        update leases set state='released', released_at=now()
                        where owner_task_id=$1 and state='active'
                        """,
                        task["id"],
                    )
                    await set_agent(conn, agent["id"], "blocked", task["id"])
                    await emit_event(
                        conn, "budget.exhausted", "task", task["id"], task["version"] + 1,
                        "service", None, task_code,
                        {"kind": "cost", "spent": float(spent), "max_cost": max_cost},
                    )
                    await emit_event(
                        conn, "task.blocked", "task", task["id"], task["version"] + 2,
                        "service", None, task_code,
                        {
                            "reason": f"cost budget exhausted ({float(spent):.6f}/{max_cost})",
                            "options": ["increase budget", "reduce scope", "handoff"],
                        },
                    )
                return {
                    "accepted": True,
                    "spent": float(spent),
                    "max_cost": max_cost or None,
                    "budget_exhausted": exhausted,
                }
            return {"accepted": True, "ignored": "unknown task"}

        if event_type == "task.blocked":
            task_code = payload.get("task")
            if not task_code:
                raise HTTPException(422, "task.blocked requires task")
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                task_code,
            )
            if not task:
                raise HTTPException(404, "task not found")
            await conn.execute(
                "update tasks set state='blocked', version=version+1, updated_at=now() where id=$1",
                task["id"],
            )
            await conn.execute(
                "update leases set state='released', released_at=now() where owner_task_id=$1 and state='active'",
                task["id"],
            )
            await set_agent(conn, agent["id"], "blocked", task["id"])
            await emit_event(
                conn, "task.blocked", "task", task["id"], task["version"] + 1, "agent", agent["id"],
                task_code, payload,
            )
            return {"accepted": True}
        await emit_event(
            conn, event_type or "agent.event", "agent", agent["id"], 1, "agent", agent["id"],
            agent_code, payload,
        )
    return {"accepted": True}
