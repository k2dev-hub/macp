"""Database pool, demo seed and event emission."""

from __future__ import annotations

import json
import os
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg

from .canonical import event_hash

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://macp@db:5432/macp")
DEMO_PROJECT_CODE = "demo"
ADVISORY_LOCK = 918273645
EVENT_CHAIN_LOCK = 918273646


def uid(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"macp:demo:{name}")


PROJECT_ID = uid("project")


@dataclass(frozen=True)
class ProjectContext:
    id: uuid.UUID
    code: str


_DEFAULT_PROJECT = ProjectContext(PROJECT_ID, DEMO_PROJECT_CODE)
_active_project: ContextVar[ProjectContext] = ContextVar(
    "macp_active_project", default=_DEFAULT_PROJECT
)


def current_project() -> ProjectContext:
    return _active_project.get()


def set_project(project: ProjectContext) -> Token:
    return _active_project.set(project)


def reset_project(token: Token) -> None:
    _active_project.reset(token)
COORDINATOR_ID = uid("agent:coordinator-1")
OBSERVER_ID = uid("agent:observer-1")
WORKER_IDS = {
    "worker-a": uid("agent:worker-a"),
    "worker-b": uid("agent:worker-b"),
    "worker-c": uid("agent:worker-c"),
    "worker-d": uid("agent:worker-d"),
    "worker-e": uid("agent:worker-e"),
    "worker-f": uid("agent:worker-f"),
    "worker-g": uid("agent:worker-g"),
    "worker-smoke": uid("agent:worker-smoke"),
    "worker-llm": uid("agent:worker-llm"),
    "worker-deepseek": uid("agent:worker-deepseek"),
}
WORKSTREAM_IDS = {
    "stream-core": uid("workstream:stream-core"),
    "stream-ui": uid("workstream:stream-ui"),
    "stream-infra": uid("workstream:stream-infra"),
}


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        DATABASE_URL,
        min_size=int(os.getenv("MACP_DB_POOL_MIN", "2")),
        max_size=int(os.getenv("MACP_DB_POOL_MAX", "25")),
        init=_init_connection,
    )


async def emit_event(
    conn: asyncpg.Connection,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    aggregate_version: int,
    actor_type: str,
    actor_id: uuid.UUID | None,
    correlation_id: str,
    payload: dict,
    session_id: str | None = None,
) -> None:
    if conn.is_in_transaction():
        await _emit_event(
            conn, event_type, aggregate_type, aggregate_id, aggregate_version,
            actor_type, actor_id, correlation_id, payload, session_id,
        )
        return
    async with conn.transaction():
        await _emit_event(
            conn, event_type, aggregate_type, aggregate_id, aggregate_version,
            actor_type, actor_id, correlation_id, payload, session_id,
        )


async def _emit_event(
    conn: asyncpg.Connection,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    aggregate_version: int,
    actor_type: str,
    actor_id: uuid.UUID | None,
    correlation_id: str,
    payload: dict,
    session_id: str | None = None,
) -> None:
    # Serialize chain extension.  Without this lock two concurrent writers can
    # read the same prev_hash and fork the audit chain.
    await conn.execute("select pg_advisory_xact_lock($1)", EVENT_CHAIN_LOCK)
    project_id = current_project().id
    prev_hash = await conn.fetchval(
        "select hash from events where project_id=$1 order by seq desc limit 1",
        project_id,
    )
    seq = await conn.fetchval("select nextval(pg_get_serial_sequence('events','seq'))")
    event_id = uuid.uuid4()
    occurred_dt = datetime.now(timezone.utc)
    occurred_at = occurred_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    body = {
        "id": str(event_id),
        "seq": seq,
        "type": event_type,
        "version": 1,
        "aggregate": {
            "type": aggregate_type,
            "id": str(aggregate_id),
            "version": aggregate_version,
        },
        "actor": {
            "type": actor_type,
            "id": str(actor_id) if actor_id else None,
            "session_id": session_id,
        },
        "correlation_id": correlation_id,
        "causation_id": None,
        "idempotency_key": f"demo:{correlation_id}:{event_type}:{uuid.uuid4()}",
        "occurred_at": occurred_at,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    digest = event_hash(prev_hash, body)
    await conn.execute(
        """
        insert into events (
          seq, id, project_id, type, version, aggregate_type, aggregate_id,
          aggregate_version, actor_type, actor_id, session_id, correlation_id,
          causation_id, idempotency_key, payload, occurred_at, prev_hash, hash
        ) values ($1,$2,$3,$4,1,$5,$6,$7,$8,$9,$10,$11,null,$12,$13::jsonb,$14::timestamptz,$15,$16)
        """,
        seq,
        event_id,
        project_id,
        event_type,
        aggregate_type,
        aggregate_id,
        aggregate_version,
        actor_type,
        actor_id,
        session_id,
        correlation_id,
        body["idempotency_key"],
        payload,
        occurred_dt,
        prev_hash,
        digest,
    )
    await conn.execute(
        """
        insert into outbox (event_id, topic) values ($1, 'macp.events')
        on conflict (event_id) do nothing
        """,
        event_id,
    )


TASK_SEEDS = [
    ("T-1001", "Add session expiry handling", "stream-core", "P1", "slice", "ready"),
    ("T-1002", "Review console diff viewer", "stream-ui", "P1", "slice", "assigned"),
    ("T-1003", "Lease conflict detection", "stream-core", "P0", "fix", "in_progress"),
    ("T-1004", "Registration gate for runtime", "stream-infra", "P1", "infra", "review"),
    ("T-1005", "Upstream schema migration", "stream-ui", "P2", "migration", "blocked"),
    ("T-1006", "Evidence viewer raw log", "stream-core", "P2", "slice", "accepted"),
    ("T-1007", "Approval inbox card", "stream-ui", "P2", "slice", "ready"),
    ("T-1008", "Backup restore drill", "stream-infra", "P2", "infra", "accepted"),
]

ASSIGNMENT_SEEDS = [
    ("T-1002", "worker-a", "offered"),
    ("T-1003", "worker-b", "active"),
    ("T-1004", "worker-c", "submitted"),
    ("T-1005", "worker-d", "active"),
]

AGENT_SEEDS = [
    ("coordinator-1", "coordinator", "idle"),
    ("observer-1", "observer", "idle"),
    ("worker-a", "worker", "working"),
    ("worker-b", "worker", "working"),
    ("worker-c", "worker", "waiting_user"),
    ("worker-d", "worker", "blocked"),
    ("worker-e", "worker", "idle"),
    ("worker-f", "worker", "idle"),
    ("worker-g", "worker", "idle"),
    ("worker-smoke", "worker", "idle"),
    ("worker-llm", "worker", "idle"),
    ("worker-deepseek", "worker", "idle"),
]


async def ensure_seed(pool: asyncpg.Pool) -> None:
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute("select pg_advisory_lock($1)", ADVISORY_LOCK)
        try:
            async with conn.transaction():
                await conn.execute(
                    """
                    create table if not exists task_documents (
                      id uuid primary key,
                      project_id uuid not null references projects(id) on delete cascade,
                      task_id uuid references tasks(id) on delete set null,
                      filename text not null,
                      media_type text not null,
                      size_bytes bigint not null,
                      sha256 text not null,
                      storage_path text not null,
                      text_content text not null,
                      char_count integer not null,
                      warnings jsonb not null default '[]'::jsonb,
                      created_by uuid,
                      created_at timestamp with time zone not null default now()
                    )
                    """
                )
                await conn.execute(
                    "create index if not exists ix_task_documents_task on task_documents(task_id)"
                )
                await conn.execute(
                    """
                    create table if not exists llm_providers (
                      id uuid primary key default gen_random_uuid(),
                      project_id uuid not null references projects(id) on delete cascade,
                      agent_code text not null,
                      name text not null,
                      base_url text not null,
                      model text not null,
                      api_key_ciphertext text,
                      price_input_per_1m numeric not null default 0,
                      price_output_per_1m numeric not null default 0,
                      price_cache_hit_per_1m numeric not null default 0,
                      enabled boolean not null default true,
                      updated_at timestamp with time zone not null default now(),
                      unique (project_id, agent_code)
                    )
                    """
                )
                await conn.execute(
                    """
                    create table if not exists project_quotas (
                      project_id uuid primary key references projects(id) on delete cascade,
                      max_active_tasks integer not null default 0,
                      max_tasks_per_day integer not null default 0,
                      max_cost_per_day numeric not null default 0
                    )
                    """
                )
                # audit_anchors shipped with a globally unique `day`, which makes
                # per-project anchoring impossible as soon as a second project
                # exists.  Migrate to (day, project_id); idempotent.
                await conn.execute(
                    "alter table audit_anchors add column if not exists project_id uuid"
                )
                await conn.execute(
                    """
                    update audit_anchors a
                       set project_id = (select p.id from projects p order by p.code limit 1)
                     where a.project_id is null
                    """
                )
                await conn.execute(
                    "alter table audit_anchors drop constraint if exists audit_anchors_day_key"
                )
                await conn.execute(
                    """
                    create unique index if not exists ux_audit_anchors_day_project
                      on audit_anchors(day, project_id)
                    """
                )
                await conn.execute(
                    "create index if not exists ix_audit_anchors_project on audit_anchors(project_id, day desc)"
                )
                await conn.execute(
                    """
                    insert into projects (id, code, name, policy)
                    values ($1, $2, 'MACP Demo', '{"demo": true}'::jsonb)
                    on conflict (code) do nothing
                    """,
                    PROJECT_ID,
                    DEMO_PROJECT_CODE,
                )
                for code, role, status in AGENT_SEEDS:
                    await conn.execute(
                        """
                        insert into agents (
                          id, project_id, code, role, model_policy, capabilities,
                          budget, status, heartbeat_at
                        ) values ($1,$2,$3,$4,'{"preference":"demo"}','{}'::text[],'{}'::jsonb,$5,$6)
                        on conflict (project_id, code) do nothing
                        """,
                        WORKER_IDS.get(code) or (COORDINATOR_ID if code == "coordinator-1" else OBSERVER_ID),
                        PROJECT_ID,
                        code,
                        role,
                        status,
                        now,
                    )

                workstreams = [
                    ("stream-core", "Core", "Task graph, state machine, gates", ["src/**", "config/core/**"]),
                    ("stream-ui", "UI", "Mission control and review console", ["web/**"]),
                    ("stream-infra", "Infra", "Runner, gates, storage", ["infra/**", "deploy/**"]),
                ]
                for code, name, responsibility, write_surface in workstreams:
                    await conn.execute(
                        """
                        insert into workstreams (
                          id, project_id, code, name, responsibility, write_surface,
                          acceptance_profile, wip_limit, owner_agent_id
                        ) values ($1,$2,$3,$4,$5,$6::jsonb,'{"levels":["L1"]}'::jsonb,2,$7)
                        on conflict (project_id, code) do nothing
                        """,
                        WORKSTREAM_IDS[code],
                        PROJECT_ID,
                        code,
                        name,
                        responsibility,
                        write_surface,
                        COORDINATOR_ID,
                    )

                hotfile = await conn.fetchval(
                    "select 1 from hotfile_policies where project_id=$1 and path_glob='shared/**' limit 1",
                    PROJECT_ID,
                )
                if not hotfile:
                    await conn.execute(
                        """
                        insert into hotfile_policies (
                          id, project_id, path_glob, lease_key, owner_workstream_id,
                          strategy, active
                        ) values ($1,$2,'shared/**','shared/',null,'single_writer',true)
                        """,
                        uid("hotfile:shared"),
                        PROJECT_ID,
                    )

                if os.getenv("MACP_DEMO_SEED_TASKS", "0") == "1":
                    for index, (code, title, stream, priority, kind, state) in enumerate(TASK_SEEDS):
                        task_id = uid(f"task:{code}")
                        created = now - timedelta(minutes=70 - index * 6)
                        assigned = now - timedelta(minutes=40 - index * 3) if state != "ready" else None
                        accepted = now - timedelta(minutes=20 - index) if state == "accepted" else None
                        await conn.execute(
                            """
                            insert into tasks (
                              id, project_id, code, workstream_id, title, kind, priority,
                              state, current_attempt, budget, acceptance_profile,
                              completion_contract, created_by, created_at, updated_at,
                              ready_at, assigned_at, accepted_at
                            ) values ($1,$2,$3,$4,$5,$6,$7,$8,1,'{}'::jsonb,'{"levels":["L1"]}'::jsonb,
                                      '{}'::jsonb,$9,$10,$11,$12,$13,$14)
                            on conflict (project_id, code) do nothing
                            """,
                            task_id,
                            PROJECT_ID,
                            code,
                            WORKSTREAM_IDS[stream],
                            title,
                            kind,
                            priority,
                            state,
                            COORDINATOR_ID,
                            created,
                            now - timedelta(minutes=index),
                            now - timedelta(minutes=60 - index * 4) if state != "ready" else None,
                            assigned,
                            accepted,
                        )

                    has_events = await conn.fetchval(
                        "select exists(select 1 from events where project_id=$1)", PROJECT_ID
                    )

                    for task_code, worker, state in ASSIGNMENT_SEEDS:
                        task_id = uid(f"task:{task_code}")
                        agent_id = WORKER_IDS[worker]
                        exists = await conn.fetchval(
                            """
                            select 1 from assignments
                            where task_id = $1 and state in ('offered','claimed','active','submitted')
                            """,
                            task_id,
                        )
                        if not has_events and not exists:
                            await conn.execute(
                                """
                                insert into assignments (
                                  id, task_id, attempt_id, agent_id, state,
                                  lease_expires_at, heartbeat_at, fencing_token, created_at, claimed_at
                                ) values ($1,$2,null,$3,$4,$5,$6,1,$7,$8)
                                on conflict (id) do nothing
                                """,
                                uid(f"assignment:{task_code}"),
                                task_id,
                                agent_id,
                                state,
                                now + timedelta(hours=1),
                                now,
                                now - timedelta(minutes=45),
                                now - timedelta(minutes=40) if state != "offered" else None,
                            )

                    if not has_events:
                        agent_tasks = {
                            "worker-a": "T-1002",
                            "worker-b": "T-1003",
                            "worker-c": "T-1004",
                            "worker-d": "T-1005",
                        }
                        for worker, task_code in agent_tasks.items():
                            await conn.execute(
                                "update agents set current_task_id=$1, heartbeat_at=$2 where id=$3",
                                uid(f"task:{task_code}"),
                                now,
                                WORKER_IDS[worker],
                            )

                    if not has_events:
                        for code, role, _ in AGENT_SEEDS:
                            agent_uuid = (
                                COORDINATOR_ID
                                if code == "coordinator-1"
                                else OBSERVER_ID
                                if code == "observer-1"
                                else WORKER_IDS[code]
                            )
                            await emit_event(
                                conn,
                                "agent.registered",
                                "agent",
                                agent_uuid,
                                1,
                                "service",
                                None,
                                code,
                                {"role": role, "model": "demo"},
                            )
                        for code, title, stream, priority, kind, state in TASK_SEEDS:
                            await emit_event(
                                conn,
                                "task.created",
                                "task",
                                uid(f"task:{code}"),
                                1,
                                "agent",
                                COORDINATOR_ID,
                                code,
                                {
                                    "title": title,
                                    "workstream": stream,
                                    "priority": priority,
                                    "kind": kind,
                                    "state": state,
                                },
                            )
                        await emit_event(
                            conn,
                            "task.assigned",
                            "task",
                            uid("task:T-1002"),
                            2,
                            "agent",
                            COORDINATOR_ID,
                            "T-1002",
                            {"agent": "worker-a", "assignment_id": str(uid("assignment:T-1002"))},
                        )
                        await emit_event(
                            conn,
                            "task.started",
                            "task",
                            uid("task:T-1003"),
                            2,
                            "agent",
                            WORKER_IDS["worker-b"],
                            "T-1003",
                            {"agent": "worker-b", "attempt": 1},
                        )
                        await emit_event(
                            conn,
                            "gate.passed",
                            "gate_run",
                            uid("gate:T-1004:build"),
                            1,
                            "service",
                            None,
                            "T-1004",
                            {"gate": "build@v1", "exit": 0, "evidence_id": "E-demo-build"},
                        )
                        await emit_event(
                            conn,
                            "task.completed",
                            "task",
                            uid("task:T-1004"),
                            2,
                            "agent",
                            WORKER_IDS["worker-c"],
                            "T-1004",
                            {"receipt_id": "R-demo-1004", "attempt": 1},
                        )
                        await emit_event(
                            conn,
                            "task.blocked",
                            "task",
                            uid("task:T-1005"),
                            2,
                            "agent",
                            WORKER_IDS["worker-d"],
                            "T-1005",
                            {"reason": "waiting for upstream schema", "options": ["A", "B", "C"]},
                        )
        finally:
            await conn.execute("select pg_advisory_unlock($1)", ADVISORY_LOCK)
