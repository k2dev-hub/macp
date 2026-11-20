"""Observer service: event-chain audit, dedupe, metrics and alerts.

Run as ``python -m server.observer`` (continuous) or ``python -m
server.observer --once``.  It deliberately uses only the database and an
optional webhook; it never mutates task state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

from .canonical import event_hash
from .db import ProjectContext, create_pool, current_project, emit_event, reset_project, set_project

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("macp.observer")

CONSUMER = "observer"
BATCH = int(os.getenv("MACP_OBSERVER_BATCH", "2000"))
INTERVAL = int(os.getenv("MACP_OBSERVER_INTERVAL", "30"))
WEBHOOK = os.getenv("MACP_ALERT_WEBHOOK", "").strip()
DAILY_COST_ALERT = float(os.getenv("MACP_DAILY_COST_ALERT", "5.0"))
SCAN_EVENT_INTERVAL = int(os.getenv("MACP_OBSERVER_EVENT_INTERVAL", "300"))
LAST_SCAN_EVENT = 0.0


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event_body(row) -> dict:
    return {
        "id": str(row["id"]),
        "seq": row["seq"],
        "type": row["type"],
        "version": row["version"],
        "aggregate": {
            "type": row["aggregate_type"],
            "id": str(row["aggregate_id"]),
            "version": row["aggregate_version"],
        },
        "actor": {
            "type": row["actor_type"],
            "id": str(row["actor_id"]) if row["actor_id"] else None,
            "session_id": str(row["session_id"]) if row["session_id"] else None,
        },
        "correlation_id": row["correlation_id"],
        "causation_id": str(row["causation_id"]) if row["causation_id"] else None,
        "idempotency_key": row["idempotency_key"],
        "occurred_at": iso(row["occurred_at"]),
        "payload": row["payload"],
        "prev_hash": row["prev_hash"],
    }


async def scan_events(conn) -> dict:
    cursor = await conn.fetchval(
        "select since_seq from observer_cursors where consumer=$1", CONSUMER
    )
    if cursor is None:
        cursor = 0
    last_hash = None
    if cursor:
        last_hash = await conn.fetchval(
            "select hash from events where project_id=$1 and seq=$2", current_project().id, cursor
        )
    rows = await conn.fetch(
        """
        select * from events where project_id=$1 and seq > $2
        order by seq asc limit $3
        """,
        current_project().id,
        cursor,
        BATCH,
    )
    checked = duplicates = broken = 0
    broken_seq = []
    for row in rows:
        body = event_body(row)
        if row["prev_hash"] != last_hash:
            broken += 1
            broken_seq.append(row["seq"])
        elif event_hash(row["prev_hash"], body) != row["hash"]:
            broken += 1
            broken_seq.append(row["seq"])
        inserted = await conn.fetchval(
            """
            insert into observer_dedupe (consumer, dedupe_key, event_id)
            values ($1,$2,$3)
            on conflict (consumer, dedupe_key) do nothing
            returning 1
            """,
            CONSUMER,
            row["hash"],
            row["id"],
        )
        if inserted is None:
            duplicates += 1
        last_hash = row["hash"]
        checked += 1
        cursor = row["seq"]
    if rows:
        await conn.execute(
            """
            insert into observer_cursors (consumer, since_seq, updated_at)
            values ($1,$2,now())
            on conflict (consumer) do update
              set since_seq=excluded.since_seq, updated_at=now()
            """,
            CONSUMER,
            cursor,
        )
    return {
        "checked": checked,
        "duplicates": duplicates,
        "broken": broken,
        "broken_seq": broken_seq,
        "cursor": cursor,
        "more": len(rows) == BATCH,
    }


async def collect_metrics(conn, consumer: str | None = None) -> dict:
    # The consumer cursor is per project ("observer:<code>").  Never rely on the
    # module-level CONSUMER here: the API process never runs the observer loop, so
    # it would read the legacy global cursor ("observer", since_seq=0) and report a
    # 12h+ lag on /metrics while the observer had actually caught up.
    consumer = consumer or f"observer:{current_project().code}"
    states = {
        row["state"]: row["count"]
        for row in await conn.fetch(
            "select state, count(*)::int as count from tasks where project_id=$1 group by state",
            current_project().id,
        )
    }
    metrics = {
        "tasks_total": sum(states.values()),
        "tasks_ready": states.get("ready", 0),
        "tasks_assigned": states.get("assigned", 0),
        "tasks_in_progress": states.get("in_progress", 0),
        "tasks_review": states.get("review", 0),
        "tasks_blocked": states.get("blocked", 0),
        "tasks_accepted": states.get("accepted", 0) + states.get("done", 0),
        "wip": states.get("assigned", 0) + states.get("in_progress", 0) + states.get("blocked", 0),
        "review_debt": states.get("review", 0),
        "blocked": states.get("blocked", 0),
        "agents_online": await conn.fetchval(
            """
            select count(*)::int from agents
            where project_id=$1 and status in ('idle','working','waiting_user')
            """,
            current_project().id,
        ),
        "agents_stale": await conn.fetchval(
            """
            select count(*)::int from agents
            where project_id=$1 and status in ('working','waiting_user')
              and (heartbeat_at is null or heartbeat_at < now()-interval '5 minutes')
            """,
            current_project().id,
        ),
        "events_5m": await conn.fetchval(
            "select count(*)::int from events where project_id=$1 and occurred_at > now()-interval '5 minutes'",
            current_project().id,
        ),
        # Consumer lag: age of the oldest event the observer has not consumed
        # yet; 0 means "caught up".  The old definition (age of the newest
        # event) reported an idle system as lagging and was reset by the alert's
        # own event, which produced a self-sustaining open/clear flap.
        "event_lag_seconds": await conn.fetchval(
            """
            select coalesce(
              extract(epoch from now() - (
                select min(e.occurred_at) from events e
                where e.project_id=$1
                  and e.seq > (
                    select c.since_seq from observer_cursors c where c.consumer=$2
                  )
              )), 0)::int
            """,
            current_project().id,
            consumer,
        ),
        "model_cost_today": float(
            await conn.fetchval(
                """
                select coalesce(sum((payload->>'cost')::numeric),0) from events
                where project_id=$1 and type='model.usage' and occurred_at::date=current_date
                """,
                current_project().id,
            )
            or 0
        ),
        "prompt_tokens_today": int(
            await conn.fetchval(
                """
                select coalesce(sum((payload->>'prompt_tokens')::bigint),0) from events
                where project_id=$1 and type='model.usage' and occurred_at::date=current_date
                """,
                current_project().id,
            )
            or 0
        ),
        "completion_tokens_today": int(
            await conn.fetchval(
                """
                select coalesce(sum((payload->>'completion_tokens')::bigint),0) from events
                where project_id=$1 and type='model.usage' and occurred_at::date=current_date
                """,
                current_project().id,
            )
            or 0
        ),
        "model_calls_today": await conn.fetchval(
            """
            select count(*)::int from events
            where project_id=$1 and type='model.usage' and occurred_at::date=current_date
            """,
            current_project().id,
        ),
        "tool_calls_1h": await conn.fetchval(
            "select count(*)::int from events where project_id=$1 and type='tool.called' and occurred_at > now()-interval '1 hour'",
            current_project().id,
        ),
        "tool_failures_1h": await conn.fetchval(
            "select count(*)::int from events where project_id=$1 and type='tool.failed' and occurred_at > now()-interval '1 hour'",
            current_project().id,
        ),
        "merge_queue_depth": await conn.fetchval(
            """
            select count(*)::int from merge_requests
            where project_id=$1 and state in ('queued','running')
            """,
            current_project().id,
        ),
        "leases_active": await conn.fetchval(
            "select count(*)::int from leases where project_id=$1 and state='active'",
            current_project().id,
        ),
        "open_alerts": await conn.fetchval(
            "select count(*)::int from alerts where project_id=$1 and state='open'",
            current_project().id,
        ),
        "gate_runs_24h": await conn.fetchval(
            "select count(*)::int from gate_runs where project_id=$1 and created_at > now()-interval '24 hours'",
            current_project().id,
        ),
        "gate_failures_24h": await conn.fetchval(
            """
            select count(*)::int from gate_runs
            where project_id=$1 and created_at > now()-interval '24 hours'
              and state in ('failed','error')
            """,
            current_project().id,
        ),
        "receipts_pending_review": await conn.fetchval(
            "select count(*)::int from receipts where project_id=$1 and status='submitted'",
            current_project().id,
        ),
        "approvals_pending": await conn.fetchval(
            "select count(*)::int from approvals where project_id=$1 and decision is null",
            current_project().id,
        ),
        "observer_cursor_lag": await conn.fetchval(
            """
            select greatest(0,
              (select coalesce(max(seq),0) from events where project_id=$1)
              - (select coalesce(since_seq,0) from observer_cursors where consumer=$2)
            )::int
            """,
            current_project().id,
            consumer,
        ),
    }
    calls = metrics["tool_calls_1h"] or 0
    metrics["tool_error_rate"] = round(metrics["tool_failures_1h"] / calls, 4) if calls else 0.0
    metrics["review_debt_ratio"] = (
        round(metrics["review_debt"] / metrics["tasks_total"], 4)
        if metrics["tasks_total"]
        else 0.0
    )
    return metrics


async def store_metrics(conn, metrics: dict) -> None:
    for name, value in metrics.items():
        await conn.execute(
            """
            insert into metrics_daily (day, project_id, metric, value, meta)
            values (current_date,$1,$2,$3,'{}'::jsonb)
            on conflict (day, project_id, metric) do update
              set value=excluded.value, meta=excluded.meta
            """,
            current_project().id,
            name,
            value,
        )


async def upsert_alert(conn, severity: str, code: str, key: str, details: dict) -> tuple[str, bool]:
    """Return ``(alert_id, opened)``; ``opened`` is True for a new alert row."""

    existing = await conn.fetchrow(
        """
        select id from alerts
        where project_id=$1 and code=$2 and state='open' and resource->>'key'=$3
        """,
        current_project().id,
        code,
        key,
    )
    if existing:
        await conn.execute(
            """
            update alerts set severity=$1, last_seen=now(), count=count+1, details=$2
            where id=$3
            """,
            severity,
            details,
            existing["id"],
        )
        return str(existing["id"]), False
    alert_id = await conn.fetchval(
        """
        insert into alerts (project_id, severity, code, resource, details)
        values ($1,$2,$3,$4,$5::jsonb) returning id
        """,
        current_project().id,
        severity,
        code,
        {"key": key},
        details,
    )
    await emit_event(
        conn,
        "alert.opened",
        "alert",
        alert_id,
        1,
        "service",
        None,
        key,
        {"severity": severity, "code": code, "details": details},
    )
    return str(alert_id), True


async def clear_alert(conn, code: str, key: str) -> int:
    rows = await conn.fetch(
        """
        update alerts set state='cleared'
        where project_id=$1 and code=$2 and state='open' and resource->>'key'=$3
        returning id
        """,
        current_project().id,
        code,
        key,
    )
    for row in rows:
        await emit_event(
            conn,
            "alert.cleared",
            "alert",
            row["id"],
            2,
            "service",
            None,
            key,
            {"code": code},
        )
    return len(rows)


async def notify(conn, event_id, alert) -> None:
    """Deliver one alert to the optional webhook, with durable dedupe."""

    dedupe = f"alert:{alert['id']}"
    existing = await conn.fetchval(
        "select 1 from observer_notifications where consumer=$1 and dedupe_key=$2",
        CONSUMER,
        dedupe,
    )
    if existing:
        return
    await conn.execute(
        """
        insert into observer_notifications (consumer, dedupe_key, event_id, channel, status)
        values ($1,$2,$3,'webhook','skipped')
        """,
        CONSUMER,
        dedupe,
        event_id,
    )
    if not WEBHOOK:
        return
    payload = json.dumps(alert, ensure_ascii=False, default=str).encode("utf-8")
    status = "sent"
    try:
        req = urllib.request.Request(
            WEBHOOK,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=5).read())
    except Exception as exc:  # network errors must not kill the observer
        status = "failed"
        log.warning("webhook delivery failed: %s", exc)
    await conn.execute(
        """
        update observer_notifications set status=$1, sent_at=now()
        where consumer=$2 and dedupe_key=$3
        """,
        status,
        CONSUMER,
        dedupe,
    )


async def evaluate_alerts(conn, metrics: dict, scan: dict) -> None:
    health: list[tuple[str, str, str, dict, bool]] = []

    chain_ok = scan["broken"] == 0
    health.append(
        (
            "critical",
            "event_chain_broken",
            "global",
            {"broken": scan["broken"], "seq": scan["broken_seq"][:10]},
            not chain_ok,
        )
    )

    lag = metrics["event_lag_seconds"]
    backlog = metrics["observer_cursor_lag"]
    health.append(
        ("critical", "event_lag", "global", {"seconds": lag, "backlog": backlog}, lag > 120)
    )
    health.append(
        ("warning", "event_lag_warn", "global", {"seconds": lag, "backlog": backlog}, 60 < lag <= 120)
    )

    stale = metrics["agents_stale"]
    health.append(("warning", "agents_stale", "global", {"count": stale}, stale > 0))

    debt = metrics["review_debt"]
    health.append(("critical", "review_debt", "global", {"count": debt}, debt > 10))
    health.append(("warning", "review_debt_warn", "global", {"count": debt}, 5 < debt <= 10))

    blocked = metrics["blocked"]
    health.append(("warning", "blocked_tasks", "global", {"count": blocked}, blocked > 5))

    rate = metrics["tool_error_rate"]
    health.append(("warning", "tool_error_rate", "global", {"rate": rate}, rate > 0.2 and metrics["tool_calls_1h"] >= 10))

    cost = metrics["model_cost_today"]
    health.append(
        ("critical" if cost > DAILY_COST_ALERT * 2 else "warning", "model_cost", "daily", {"cost": cost}, cost > DAILY_COST_ALERT)
    )

    pending = metrics["approvals_pending"]
    health.append(("warning", "approvals_pending", "global", {"count": pending}, pending > 3))

    cursor_lag = metrics["observer_cursor_lag"]
    health.append(
        ("warning", "observer_lag", "global", {"events": cursor_lag}, cursor_lag > 2000)
    )

    for severity, code, key, details, triggered in health:
        if triggered:
            alert_id, opened = await upsert_alert(conn, severity, code, key, details)
            if opened:
                alert = {
                    "id": alert_id,
                    "severity": severity,
                    "code": code,
                    "resource": {"key": key},
                    "details": details,
                }
                event_id = await conn.fetchval(
                    "select id from events where project_id=$1 order by seq desc limit 1",
                    current_project().id,
                )
                await notify(conn, event_id, alert)
        else:
            await clear_alert(conn, code, key)


async def _run_project(pool) -> dict:
    import time

    global LAST_SCAN_EVENT
    async with pool.acquire() as conn:
        async with conn.transaction():
            scan = await scan_events(conn)
        async with conn.transaction():
            metrics = await collect_metrics(conn)
            await store_metrics(conn, metrics)
            await evaluate_alerts(conn, metrics, scan)
            summary = {
                "cursor": scan["cursor"],
                "events_checked": scan["checked"],
                "duplicates": scan["duplicates"],
                "broken": scan["broken"],
                "metrics": {
                    key: metrics[key]
                    for key in (
                        "tasks_total",
                        "wip",
                        "review_debt",
                        "blocked",
                        "event_lag_seconds",
                        "model_cost_today",
                        "tool_error_rate",
                        "open_alerts",
                    )
                },
            }
            if time.monotonic() - LAST_SCAN_EVENT > SCAN_EVENT_INTERVAL:
                LAST_SCAN_EVENT = time.monotonic()
                await emit_event(
                    conn,
                    "observer.scanned",
                    "project",
                    current_project().id,
                    1,
                    "service",
                    None,
                    "observer",
                    summary,
                )
    return summary


async def main() -> None:
    pool = await create_pool()
    log.info("observer started (interval=%ss, webhook=%s)", INTERVAL, bool(WEBHOOK))
    try:
        while True:
            try:
                await touch_observer(pool)
                summaries = await run_once(pool)
                for summary in summaries:
                    log.info(
                        "[%s] cursor=%s checked=%s broken=%s alerts=%s",
                        summary.get("project"),
                        summary["cursor"],
                        summary["events_checked"],
                        summary["broken"],
                        summary["metrics"]["open_alerts"],
                    )
            except Exception:
                log.exception("observer scan failed")
            await asyncio.sleep(INTERVAL)
    finally:
        await pool.close()


async def touch_observer(pool) -> None:
    """Observer runs outside the agent image, so it reports liveness by SQL."""

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "update agents set heartbeat_at=now(), "
                "status = case when status in ('offline','dead') then 'idle' else status end "
                "where code = 'observer-1'"
            )
    except Exception:
        pass


async def run_once(pool) -> list[dict]:
    global CONSUMER
    async with pool.acquire() as conn:
        projects = await conn.fetch("select id, code from projects order by code")
    summaries = []
    for project in projects:
        CONSUMER = f"observer:{project['code']}"
        token = set_project(ProjectContext(project["id"], project["code"]))
        try:
            summary = await _run_project(pool)
            summary["project"] = project["code"]
            summaries.append(summary)
        finally:
            reset_project(token)
    return summaries


if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        async def once() -> None:
            pool = await create_pool()
            try:
                print(json.dumps(await run_once(pool), default=str, ensure_ascii=False))
            finally:
                await pool.close()

        asyncio.run(once())
    else:
        asyncio.run(main())
