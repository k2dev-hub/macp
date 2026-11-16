"""MACP M0 read-only viewer API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .db import ProjectContext, current_project, create_pool, ensure_seed, reset_project, set_project
from . import signing
from .auth import me_router as auth_router
from .agent_api import router as agent_router
from .admin_api import router as admin_router
from .document_api import router as document_router
from .provenance_api import router as provenance_router
from .tool_registry import router as tool_router
from .merge_api import router as merge_router
from .settings import SETTINGS as settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    await ensure_seed(app.state.pool)
    token = os.getenv("MACP_WRITE_TOKEN", "").strip()
    token_file = os.getenv("MACP_WRITE_TOKEN_FILE")
    if not token and token_file and Path(token_file).exists():
        token = Path(token_file).read_text(encoding="utf-8").strip()
    app.state.write_token = token
    yield
    await app.state.pool.close()


APP_VERSION = os.getenv("MACP_VERSION", "0.2.0")
OIDC_ISSUER = os.getenv("OIDC_ISSUER", settings.oidc_issuer).rstrip("/")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "macp")
OIDC_INTERNAL_URL = os.getenv("OIDC_INTERNAL_URL", "http://oidc:9100").rstrip("/")
OIDC_CLIENT_SECRET_FILE = os.getenv(
    "OIDC_CLIENT_SECRET_FILE", "/run/secrets/oidc_client_secret"
)
app = FastAPI(title="MACP Mission Control", version=APP_VERSION, lifespan=lifespan)
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(admin_router)
app.include_router(document_router)
app.include_router(provenance_router)
app.include_router(tool_router)
app.include_router(merge_router)

log = logging.getLogger("macp.api")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@app.middleware("http")
async def project_context(request: Request, call_next):
    code = (
        request.headers.get("x-macp-project")
        or request.query_params.get("project")
        or "demo"
    ).strip() or "demo"
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select id, code from projects where code=$1", code
        )
    if not row:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"detail": {"code": "project_not_found", "detail": code}}, status_code=404
        )
    token = set_project(ProjectContext(row["id"], row["code"]))
    try:
        return await call_next(request)
    finally:
        reset_project(token)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("request %s %s failed", request.method, request.url.path)
        raise
    elapsed_ms = int((time.monotonic() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/v1") and "/stream" not in request.url.path:
        log.info(
            "%s %s %s %sms rid=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
    return response


def pool(request: Request):
    return request.app.state.pool


@app.get("/v1/auth/config")
async def auth_config():
    return {
        "issuer": OIDC_ISSUER,
        "client_id": OIDC_CLIENT_ID,
        "scope": "openid profile email",
        "mode": "oidc",
    }


@app.post("/v1/auth/token")
async def auth_token(body: dict):
    code = body.get("code")
    redirect_uri = body.get("redirect_uri")
    if not code or not redirect_uri:
        raise HTTPException(422, "code and redirect_uri are required")
    secret = Path(OIDC_CLIENT_SECRET_FILE).read_text(encoding="utf-8").strip()
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": OIDC_CLIENT_ID,
            "client_secret": secret,
        }
    ).encode("utf-8")
    request_obj = urllib.request.Request(
        OIDC_INTERNAL_URL + "/token",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HTTPException(401, exc.read().decode("utf-8", "replace")) from exc


@app.get("/v1/version")
async def version():
    return {
        "name": "macp",
        "version": APP_VERSION,
        "build": os.getenv("MACP_BUILD", "local"),
        "openapi": "/openapi.json",
        "docs": "/docs",
    }


@app.get("/v1/health")
async def health(request: Request):
    async with pool(request).acquire() as conn:
        await conn.fetchval("select 1")
    return {"status": "ok", "db": "ok", "demo": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/v1/overview")
async def overview(request: Request):
    async with pool(request).acquire() as conn:
        state_rows = await conn.fetch(
            "select state, count(*)::int as count from tasks where project_id=$1 group by state",
            current_project().id,
        )
        states = {row["state"]: row["count"] for row in state_rows}
        execution_wip = sum(states.get(s, 0) for s in ("assigned", "in_progress", "blocked"))
        review_debt = states.get("review", 0)
        accepted = states.get("accepted", 0) + states.get("done", 0)
        agents_online = await conn.fetchval(
            """
            select count(*)::int from agents
            where project_id=$1 and status in ('idle','working','waiting_user')
            """,
            current_project().id,
        )
        events_5m = await conn.fetchval(
            "select count(*)::int from events where project_id=$1 and occurred_at > now()-interval '5 minutes'",
            current_project().id,
        )
        last_event = await conn.fetchval(
            "select max(occurred_at) from events where project_id=$1", current_project().id
        )
        workstreams = await conn.fetch(
            """
            select w.code, w.name, w.wip_limit,
                   count(t.id) filter (where t.state in ('assigned','in_progress','blocked'))::int as wip
            from workstreams w
            left join tasks t on t.workstream_id = w.id
            where w.project_id=$1
            group by w.id
            order by w.code
            """,
            current_project().id,
        )
        daily = {
            row["metric"]: float(row["value"])
            for row in await conn.fetch(
                "select metric, value from metrics_daily where project_id=$1 and day=current_date",
                current_project().id,
            )
        }
        open_alerts = await conn.fetchval(
            "select count(*)::int from alerts where project_id=$1 and state='open'",
            current_project().id,
        )
    return {
        "demo": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_wip": execution_wip,
        "review_debt": review_debt,
        "blocked": states.get("blocked", 0),
        "accepted": accepted,
        "agents_online": agents_online,
        "events_5m": events_5m,
        "last_event_at": last_event.isoformat() if last_event else None,
        "model_cost_today": daily.get("model_cost_today", 0.0),
        "open_alerts": open_alerts,
        "event_lag_seconds": daily.get("event_lag_seconds", 0),
        "tool_error_rate": daily.get("tool_error_rate", 0.0),
        "states": states,
        "workstreams": [dict(row) for row in workstreams],
    }


@app.get("/v1/tasks")
async def tasks(request: Request):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select t.code, t.title, t.state, t.priority, t.kind, t.current_attempt,
                   t.created_at, t.updated_at, w.code as workstream,
                   ag.code as assignee, a.state as assignment_state,
                   coalesce(dep.codes, '{}') as dependencies,
                   coalesce(dep.met, true) as dependencies_met
            from tasks t
            join workstreams w on w.id = t.workstream_id
            left join lateral (
              select a.agent_id, a.state
              from assignments a
              where a.task_id = t.id and a.state in ('offered','claimed','active','submitted')
              order by a.created_at desc
              limit 1
            ) a on true
            left join agents ag on ag.id = a.agent_id
            left join lateral (
              select array_agg(d2.code order by d2.code) as codes,
                     bool_and(d2.state in ('merged','done')) as met
              from task_dependencies td
              join tasks d2 on d2.id = td.depends_on_task_id
              where td.task_id = t.id
            ) dep on true
            where t.project_id=$1
            order by
              case t.priority when 'P0' then 0 when 'P1' then 1 when 'P2' then 2 else 3 end,
              t.updated_at desc
            """,
            current_project().id,
        )
    return [dict(row) for row in rows]


@app.get("/v1/tasks/{code}")
async def task_detail(code: str, request: Request):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            select t.id, t.code, t.title, t.state, t.priority, t.kind, t.current_attempt,
                   t.budget, t.active_work_order_id, t.created_at, t.updated_at,
                   t.accepted_at, w.code as workstream,
                   ag.code as assignee, a.state as assignment_state
            from tasks t
            join workstreams w on w.id = t.workstream_id
            left join lateral (
              select a.agent_id, a.state
              from assignments a
              where a.task_id = t.id and a.state in ('offered','claimed','active','submitted')
              order by a.created_at desc
              limit 1
            ) a on true
            left join agents ag on ag.id = a.agent_id
            where t.project_id=$1 and t.code=$2
            """,
            current_project().id,
            code,
        )
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        work_order = None
        if row["active_work_order_id"]:
            wo = await conn.fetchrow(
                "select version, body, body_hash from work_orders where id=$1",
                row["active_work_order_id"],
            )
            if wo:
                work_order = {
                    "version": wo["version"],
                    "hash": wo["body_hash"],
                    **wo["body"],
                }
        events = await conn.fetch(
            """
            select seq, type, actor_type, actor_id, payload, occurred_at
            from events where project_id=$1 and correlation_id=$2
            order by seq desc limit 20
            """,
            current_project().id,
            code,
        )
        receipt = await conn.fetchrow(
            """
            select code, status, body, base_commit, candidate_ref, stale_base, created_at
            from receipts where project_id=$1 and task_id=$2
            order by created_at desc limit 1
            """,
            current_project().id,
            row["id"],
        )
        lease_rows = await conn.fetch(
            """
            select id, path_glob, state, expires_at, owner_task_id
            from leases where project_id=$1 and owner_task_id=$2 and state='active'
            """,
            current_project().id,
            row["id"],
        )
        document_rows = await conn.fetch(
            """
            select id, filename, media_type, size_bytes, sha256, char_count,
                   warnings, created_at
            from task_documents where project_id=$1 and task_id=$2
            order by created_at
            """,
            current_project().id,
            row["id"],
        )
    return {
        **dict(row),
        "work_order": work_order,
        "events": [dict(e) for e in events],
        "receipt": dict(receipt) if receipt else None,
        "leases": [dict(l) for l in lease_rows],
        "documents": [dict(d) for d in document_rows],
    }


@app.get("/v1/receipts/{code}/verify")
async def verify_receipt(code: str, request: Request):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            select r.body_hash, r.signature, r.candidate_ref, r.body,
                   ag.code as agent
            from receipts r join agents ag on ag.id=r.agent_id
            where r.project_id=$1 and r.code=$2
            """,
            current_project().id,
            code,
        )
    if not row:
        raise HTTPException(404, "receipt not found")
    valid = signing.verify(row["agent"], row["body_hash"], row["signature"] or "")
    return {
        "receipt": code,
        "agent": row["agent"],
        "valid": valid,
        "signature": row["signature"],
        "candidate_commit": row["body"].get("candidate_commit"),
    }


@app.get("/v1/agents")
async def agents(request: Request):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select a.code, a.role, a.status, a.model_policy, a.heartbeat_at,
                   t.code as current_task
            from agents a
            left join tasks t on t.id = a.current_task_id
            where a.project_id=$1
            order by case a.role when 'coordinator' then 0 when 'observer' then 1 else 2 end, a.code
            """,
            current_project().id,
        )
    return [dict(row) for row in rows]


@app.get("/v1/metrics")
async def metrics(request: Request):
    async with pool(request).acquire() as conn:
        event_rate = await conn.fetch(
            """
            select to_char(date_trunc('minute', occurred_at), 'HH24:MI') as minute,
                   count(*)::int as count
            from events
            where project_id=$1 and occurred_at > now()-interval '30 minutes'
            group by 1 order by 1
            """,
            current_project().id,
        )
        state_rows = await conn.fetch(
            "select state, count(*)::int as count from tasks where project_id=$1 group by state",
            current_project().id,
        )
        workstream_rows = await conn.fetch(
            """
            select w.code, w.name, w.wip_limit,
                   count(t.id) filter (where t.state in ('assigned','in_progress','blocked'))::int as wip
            from workstreams w
            left join tasks t on t.workstream_id = w.id
            where w.project_id=$1
            group by w.id order by w.code
            """,
            current_project().id,
        )
        type_rows = await conn.fetch(
            """
            select type, count(*)::int as count
            from events
            where project_id=$1 and occurred_at > now()-interval '1 hour'
            group by type order by count desc limit 8
            """,
            current_project().id,
        )
    return {
        "event_rate": [dict(row) for row in event_rate],
        "states": [dict(row) for row in state_rows],
        "workstreams": [dict(row) for row in workstream_rows],
        "event_types": [dict(row) for row in type_rows],
    }


@app.get("/metrics")
async def prometheus_metrics(request: Request):
    """Prometheus text exposition (aggregates only, no task content).

    Scraped by Prometheus/Grafana; kept on the same single-domain nginx entry so
    no extra port or hostname is exposed (see docs/HARDENING).
    """

    from .observer import collect_metrics

    # The consumer cursor is per project; the API process never runs the observer
    # loop, so it must pass it explicitly (otherwise /metrics reads the legacy
    # global cursor 0 and reports a permanently huge lag).
    consumer = f"observer:{current_project().code}"
    async with pool(request).acquire() as conn:
        m = await collect_metrics(conn, consumer)
        states = await conn.fetch(
            "select state, count(*)::int as count from tasks where project_id=$1 group by state",
            current_project().id,
        )
        streams = await conn.fetch(
            """
            select w.code,
                   count(t.id) filter (
                       where t.state in ('assigned','in_progress','blocked')
                   )::int as wip,
                   w.wip_limit
            from workstreams w
            left join tasks t on t.workstream_id = w.id
            where w.project_id=$1
            group by w.id order by w.code
            """,
            current_project().id,
        )

    project = current_project().code
    lines: list[str] = []

    def gauge(name: str, value, labels: str = "") -> None:
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{labels} {value}")

    gauge("macp_up", 1, f'{{project="{project}"}}')
    gauge("macp_health", 1, f'{{project="{project}",component="db"}}')
    lines.append("# TYPE macp_tasks gauge")
    for row in states:
        lines.append(
            f'macp_tasks{{project="{project}",state="{row["state"]}"}} {row["count"]}'
        )
    gauge("macp_tasks_total", m["tasks_total"], f'{{project="{project}"}}')
    gauge("macp_wip", m["wip"], f'{{project="{project}"}}')
    gauge("macp_review_debt", m["review_debt"], f'{{project="{project}"}}')
    gauge("macp_blocked", m["blocked"], f'{{project="{project}"}}')
    gauge("macp_accepted", m["tasks_accepted"], f'{{project="{project}"}}')
    gauge("macp_agents_online", m["agents_online"], f'{{project="{project}"}}')
    gauge("macp_agents_stale", m["agents_stale"], f'{{project="{project}"}}')
    gauge("macp_events_5m", m["events_5m"], f'{{project="{project}"}}')
    gauge("macp_event_lag_seconds", m["event_lag_seconds"], f'{{project="{project}"}}')
    gauge("macp_observer_cursor_lag", m["observer_cursor_lag"], f'{{project="{project}"}}')
    gauge("macp_model_cost_today_cny", m["model_cost_today"], f'{{project="{project}"}}')
    gauge("macp_prompt_tokens_today", m["prompt_tokens_today"], f'{{project="{project}"}}')
    gauge("macp_completion_tokens_today", m["completion_tokens_today"], f'{{project="{project}"}}')
    gauge("macp_model_calls_today", m["model_calls_today"], f'{{project="{project}"}}')
    gauge("macp_tool_calls_1h", m["tool_calls_1h"], f'{{project="{project}"}}')
    gauge("macp_tool_error_rate", m["tool_error_rate"], f'{{project="{project}"}}')
    gauge("macp_merge_queue_depth", m["merge_queue_depth"], f'{{project="{project}"}}')
    gauge("macp_leases_active", m["leases_active"], f'{{project="{project}"}}')
    gauge("macp_open_alerts", m["open_alerts"], f'{{project="{project}"}}')
    gauge("macp_gate_failures_24h", m["gate_failures_24h"], f'{{project="{project}"}}')
    gauge("macp_receipts_pending_review", m["receipts_pending_review"], f'{{project="{project}"}}')
    gauge("macp_approvals_pending", m["approvals_pending"], f'{{project="{project}"}}')
    lines.append("# TYPE macp_workstream_wip gauge")
    for row in streams:
        lines.append(
            f'macp_workstream_wip{{project="{project}",workstream="{row["code"]}"}} {row["wip"]}'
        )
    lines.append("# TYPE macp_workstream_wip_limit gauge")
    for row in streams:
        lines.append(
            f'macp_workstream_wip_limit{{project="{project}",workstream="{row["code"]}"}} {row["wip_limit"]}'
        )
    # Host-level gauges that the audit asked for: WAL archive growth and disk.
    import shutil as _shutil

    wal_dir = os.getenv("MACP_WAL_ARCHIVE_DIR", "/opt/macp/wal-archive")
    wal_bytes = 0
    try:
        for path in Path(wal_dir).rglob("*"):
            if path.is_file():
                wal_bytes += path.stat().st_size
    except OSError:
        pass
    gauge("macp_wal_archive_bytes", wal_bytes, f'{{project="{project}"}}')
    try:
        usage = _shutil.disk_usage(wal_dir if Path(wal_dir).exists() else "/")
        gauge("macp_disk_free_bytes", usage.free, f'{{project="{project}"}}')
        gauge("macp_disk_total_bytes", usage.total, f'{{project="{project}"}}')
    except OSError:
        pass
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/v1/events")
async def events(request: Request, since: int = 0, limit: int = Query(default=80, ge=1, le=200)):
    async with pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select e.seq, e.type, e.aggregate_type, e.actor_type, e.actor_id,
                   e.payload, e.occurred_at, t.code as task
            from events e
            left join tasks t on t.code = e.correlation_id
            where e.project_id=$1 and e.seq > $2
            order by e.seq desc limit $3
            """,
            current_project().id,
            since,
            limit,
        )
    return [dict(row) for row in rows]


@app.get("/v1/events/stream")
async def event_stream(request: Request, since: int = 0):
    last_event_id = request.headers.get("last-event-id")
    start = int(last_event_id) if last_event_id and last_event_id.isdigit() else since
    app_pool = pool(request)

    async def generate():
        last = start
        while True:
            if await request.is_disconnected():
                break
            async with app_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    select e.seq, e.type, e.actor_type, e.actor_id, e.payload,
                           e.occurred_at, t.code as task
                    from events e
                    left join tasks t on t.code = e.correlation_id
                    where e.project_id=$1 and e.seq > $2
                    order by e.seq asc limit 50
                    """,
                    current_project().id,
                    last,
                )
            for row in rows:
                last = row["seq"]
                data = {
                    "seq": row["seq"],
                    "type": row["type"],
                    "actor_type": row["actor_type"],
                    "actor_id": str(row["actor_id"]) if row["actor_id"] else None,
                    "payload": row["payload"],
                    "occurred_at": row["occurred_at"].isoformat(),
                    "task": row["task"],
                }
                yield f"id: {last}\nevent: {row['type']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if not rows:
                yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
