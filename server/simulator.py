"""Demo simulator: advances a small task graph and emits real events.

This is explicitly a demo driver, not a production agent adapter. It exercises
the same tables and event envelope that real workers will use.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone

from .db import (
    COORDINATOR_ID,
    PROJECT_ID,
    WORKER_IDS,
    create_pool,
    emit_event,
    ensure_seed,
    uid,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("macp.simulator")
MAX_TASKS = 20


async def set_agent(conn, agent_id, status: str, task_id: uuid.UUID | None) -> None:
    await conn.execute(
        "update agents set status=$1, current_task_id=$2, heartbeat_at=now() where id=$3",
        status,
        task_id,
        agent_id,
    )


async def idle_worker(conn):
    return await conn.fetchrow(
        """
        select id, code from agents
        where project_id=$1 and role='worker' and status='idle'
        order by code limit 1
        """,
        PROJECT_ID,
    )


async def create_task(conn) -> None:
    count = await conn.fetchval(
        "select count(*)::int from tasks where project_id=$1 and code not like 'T-9%'",
        PROJECT_ID,
    )
    code = f"T-{1001 + count}"
    task_id = uid(f"task:{code}")
    now = datetime.now(timezone.utc)
    await conn.execute(
        """
        insert into tasks (
          id, project_id, code, workstream_id, title, kind, priority, state,
          current_attempt, budget, acceptance_profile, completion_contract,
          created_by, created_at, updated_at, ready_at
        ) values ($1,$2,$3,$4,$5,'slice','P2','ready',1,'{}'::jsonb,
                  '{"levels":["L1"]}'::jsonb,'{}'::jsonb,$6,$7,$7,$7)
        """,
        task_id,
        PROJECT_ID,
        code,
        uid("workstream:stream-core"),
        f"Demo follow-up {count + 1}",
        COORDINATOR_ID,
        now,
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
        {"title": f"Demo follow-up {count + 1}", "workstream": "stream-core", "state": "ready"},
    )
    log.info("created %s", code)


async def recycle_task(conn) -> None:
    task = await conn.fetchrow(
        """
        select id, code from tasks
        where project_id=$1 and state='accepted' and code not like 'T-9%'
        order by updated_at asc limit 1
        """,
        PROJECT_ID,
    )
    if not task:
        return
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
        conn,
        "demo.task_recycled",
        "task",
        task["id"],
        version,
        "service",
        None,
        task["code"],
        {"reason": "demo pool limit reached", "max_tasks": MAX_TASKS},
    )
    log.info("recycled %s", task["code"])


async def tick(pool) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                """
                select t.id, t.code, t.state, t.version, t.workstream_id,
                       a.id as assignment_id, a.agent_id, a.state as assignment_state
                from tasks t
                left join lateral (
                  select a.id, a.agent_id, a.state
                  from assignments a
                  where a.task_id=t.id and a.state in ('offered','claimed','active','submitted')
                  order by a.created_at desc limit 1
                ) a on true
                where t.project_id=$1
                  and t.state in ('ready','assigned','in_progress','review','blocked')
                  and t.code not like 'T-9%'
                order by random() limit 1
                """,
                PROJECT_ID,
            )
            if not task:
                count = await conn.fetchval(
                    "select count(*)::int from tasks where project_id=$1 and code not like 'T-9%'",
                    PROJECT_ID,
                )
                if count < MAX_TASKS:
                    await create_task(conn)
                else:
                    await recycle_task(conn)
                return

            task_id = task["id"]
            code = task["code"]
            state = task["state"]
            agent_id = task["agent_id"]

            if state == "ready":
                worker = await idle_worker(conn)
                if not worker:
                    return
                agent_id = worker["id"]
                version = await conn.fetchval(
                    "update tasks set state='assigned', version=version+1, assigned_at=now(), updated_at=now() where id=$1 returning version",
                    task_id,
                )
                await conn.execute(
                    """
                    insert into assignments (task_id, agent_id, state, lease_expires_at, heartbeat_at, fencing_token)
                    values ($1,$2,'offered',now()+interval '30 minutes',now(),1)
                    """,
                    task_id,
                    agent_id,
                )
                await set_agent(conn, agent_id, "working", task_id)
                await emit_event(
                    conn, "task.assigned", "task", task_id, version, "agent", COORDINATOR_ID,
                    code, {"agent": worker["code"], "attempt": 1},
                )
                await emit_event(
                    conn, "assignment.offered", "assignment", task_id, 1, "service", None,
                    code, {"agent": worker["code"], "expires_in": 1800},
                )
                log.info("%s ready -> assigned (%s)", code, worker["code"])

            elif state == "assigned":
                version = await conn.fetchval(
                    "update tasks set state='in_progress', version=version+1, updated_at=now() where id=$1 returning version",
                    task_id,
                )
                if task["assignment_id"]:
                    await conn.execute(
                        "update assignments set state='active', claimed_at=now(), heartbeat_at=now() where id=$1",
                        task["assignment_id"],
                    )
                if agent_id:
                    await set_agent(conn, agent_id, "working", task_id)
                await emit_event(
                    conn, "assignment.claimed", "assignment", task_id, 1, "agent", agent_id,
                    code, {"attempt": 1},
                )
                await emit_event(
                    conn, "task.started", "task", task_id, version, "agent", agent_id,
                    code, {"attempt": 1},
                )
                log.info("%s assigned -> in_progress", code)

            elif state == "in_progress":
                version = await conn.fetchval(
                    "update tasks set state='review', version=version+1, updated_at=now() where id=$1 returning version",
                    task_id,
                )
                if task["assignment_id"]:
                    await conn.execute(
                        "update assignments set state='submitted', heartbeat_at=now() where id=$1",
                        task["assignment_id"],
                    )
                if agent_id:
                    await set_agent(conn, agent_id, "waiting_user", task_id)
                await emit_event(
                    conn, "gate.passed", "gate_run", uid(f"gate:{code}:build"), 1, "service", None,
                    code, {"gate": "build@v1", "exit": 0, "evidence_id": f"E-demo-{code}"},
                )
                await emit_event(
                    conn, "task.completed", "task", task_id, version, "agent", agent_id,
                    code, {"receipt_id": f"R-demo-{code}", "attempt": 1},
                )
                log.info("%s in_progress -> review", code)

            elif state == "review":
                if random.random() < 0.75:
                    version = await conn.fetchval(
                        "update tasks set state='accepted', version=version+1, accepted_at=now(), updated_at=now() where id=$1 returning version",
                        task_id,
                    )
                    if task["assignment_id"]:
                        await conn.execute(
                            "update assignments set state='closed', closed_at=now() where id=$1",
                            task["assignment_id"],
                        )
                    if agent_id:
                        await set_agent(conn, agent_id, "idle", None)
                    review_id = uid(f"review:{code}")
                    await emit_event(
                        conn, "review.accepted", "review", review_id, 1, "agent", COORDINATOR_ID,
                        code, {"receipt_id": f"R-demo-{code}", "reviewer": "coordinator-1"},
                    )
                    await emit_event(
                        conn, "task.accepted", "task", task_id, version, "agent", COORDINATOR_ID,
                        code, {"review_id": str(review_id)},
                    )
                    log.info("%s review -> accepted", code)
                else:
                    version = await conn.fetchval(
                        "update tasks set state='in_progress', version=version+1, updated_at=now() where id=$1 returning version",
                        task_id,
                    )
                    if task["assignment_id"]:
                        await conn.execute(
                            "update assignments set state='active', heartbeat_at=now() where id=$1",
                            task["assignment_id"],
                        )
                    if agent_id:
                        await set_agent(conn, agent_id, "working", task_id)
                    await emit_event(
                        conn, "review.rejected", "review", uid(f"review:{code}"), 1, "agent", COORDINATOR_ID,
                        code, {"finding": "add a TTL boundary assertion", "must_fix": True},
                    )
                    await emit_event(
                        conn, "task.rejected", "task", task_id, version, "agent", COORDINATOR_ID,
                        code, {"reason": "review rejected", "attempt": 2},
                    )
                    log.info("%s review -> rejected", code)

            elif state == "blocked":
                version = await conn.fetchval(
                    "update tasks set state='in_progress', version=version+1, updated_at=now() where id=$1 returning version",
                    task_id,
                )
                if task["assignment_id"]:
                    await conn.execute(
                        "update assignments set state='active', heartbeat_at=now() where id=$1",
                        task["assignment_id"],
                    )
                if agent_id:
                    await set_agent(conn, agent_id, "working", task_id)
                await emit_event(
                    conn, "task.resumed", "task", task_id, version, "agent", COORDINATOR_ID,
                    code, {"reason": "upstream unblocked"},
                )
                log.info("%s blocked -> in_progress", code)


async def main() -> None:
    pool = await create_pool()
    await ensure_seed(pool)
    log.info("simulator started")
    while True:
        try:
            await tick(pool)
        except Exception:
            log.exception("tick failed")
        await asyncio.sleep(2.5)


if __name__ == "__main__":
    asyncio.run(main())
