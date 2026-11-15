"""Archive legacy demo tasks that predate Work Orders.

These rows make the live queue look unhealthy and cannot be merged because they
have no Work Order, candidate ref or real base commit.  The audit events stay in
place; only the task state is changed to ``cancelled``.

Usage: python -m server.archive_legacy --apply
"""

from __future__ import annotations

import asyncio
import sys

from .db import PROJECT_ID, create_pool, emit_event
from .tool_registry import cleanup_task_worktrees


async def run(apply: bool, tests: bool = False) -> None:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if tests:
                    rows = await conn.fetch(
                        """
                        select id, code, state, version from tasks
                        where project_id=$1 and state not in ('cancelled','done','merged')
                          and (
                            title ilike '%smoke%'
                            or title ilike '%negative test%'
                            or title ilike '%L3 deploy change%'
                            or title ilike '%L2 tests change%'
                            or title ilike '%WO negative%'
                          )
                        order by code
                        """,
                        PROJECT_ID,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        select id, code, state, version from tasks
                        where project_id=$1 and active_work_order_id is null
                          and state not in ('cancelled','done')
                        order by code
                        """,
                        PROJECT_ID,
                    )
                print(f"legacy_tasks={len(rows)}")
                if not apply:
                    print("dry run; pass --apply to cancel them")
                    return
                for row in rows:
                    await conn.execute(
                        """
                        update assignments set state='closed', closed_at=now()
                        where task_id=$1 and state in ('offered','claimed','active','submitted')
                        """,
                        row["id"],
                    )
                    await conn.execute(
                        """
                        update leases set state='released', released_at=now()
                        where owner_task_id=$1 and state='active'
                        """,
                        row["id"],
                    )
                    await conn.execute(
                        """
                        update agents set status='idle', current_task_id=null
                        where current_task_id=$1
                        """,
                        row["id"],
                    )
                    version = await conn.fetchval(
                        """
                        update tasks set state='cancelled', version=version+1, updated_at=now()
                        where id=$1 returning version
                        """,
                        row["id"],
                    )
                    await emit_event(
                        conn,
                        "task.cancelled",
                        "task",
                        row["id"],
                        version,
                        "human",
                        None,
                        row["code"],
                        {
                            "reason": "test artifact archived"
                            if tests
                            else "legacy demo task without Work Order"
                        },
                    )
                for row in rows:
                    cleanup_task_worktrees(row["code"])
                print(f"cancelled={len(rows)}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv, "--tests" in sys.argv))
