"""One-off repair for a forked event chain.

The pre-production writer could fork the hash chain under concurrency because
chain extension was not serialized.  New writes are serialized with an advisory
transaction lock.  This tool re-hashes existing rows in sequence order so the
stored audit chain is internally consistent again.

Usage:  python -m server.repair_chain --apply
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from .canonical import event_hash
from .db import EVENT_CHAIN_LOCK, ProjectContext, create_pool, set_project


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def body(row) -> dict:
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


async def run(apply: bool) -> None:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            projects = await conn.fetch("select id, code from projects order by code")
        for project in projects:
            set_project(ProjectContext(project["id"], project["code"]))
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("select pg_advisory_xact_lock($1)", EVENT_CHAIN_LOCK)
                    rows = await conn.fetch(
                        "select * from events where project_id=$1 order by seq asc",
                        project["id"],
                    )
                    prev = None
                    changed = 0
                    for row in rows:
                        data = body(row)
                        new_prev = prev
                        new_hash = event_hash(new_prev, {**data, "prev_hash": new_prev})
                        if row["prev_hash"] != new_prev or row["hash"] != new_hash:
                            changed += 1
                            if apply:
                                await conn.execute(
                                    "update events set prev_hash=$1, hash=$2 where seq=$3",
                                    new_prev,
                                    new_hash,
                                    row["seq"],
                                )
                        prev = new_hash
                    consumer = f"observer:{project['code']}"
                    if apply:
                        await conn.execute(
                            "update observer_cursors set since_seq=0, updated_at=now() where consumer=$1",
                            consumer,
                        )
                        await conn.execute(
                            "delete from observer_dedupe where consumer=$1", consumer
                        )
                    print(
                        f"project={project['code']} events={len(rows)} changed={changed} "
                        f"apply={apply} head={prev}"
                    )
    finally:
        await pool.close()


if __name__ == "__main__":
    if "--apply" not in sys.argv:
        print("dry run; pass --apply to rewrite hashes")
    asyncio.run(run("--apply" in sys.argv))
