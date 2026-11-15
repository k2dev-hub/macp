"""Remove task worktrees for terminal tasks and orphan directories.

Candidate refs and commits stay in the bare repository, so history and audit
remain available.  Only the per-attempt checkout directories are removed.

Usage: python -m server.cleanup_worktrees [--apply]
"""

from __future__ import annotations

import asyncio
import sys

from .db import PROJECT_ID, create_pool
from .tool_registry import WORKTREE_ROOT, cleanup_task_worktrees, ensure_repo


async def run(apply: bool) -> None:
    ensure_repo()
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select code from tasks
                where project_id=$1 and state in ('merged','cancelled','done','failed')
                """,
                PROJECT_ID,
            )
            active = {
                row["code"]
                for row in await conn.fetch(
                    """
                    select code from tasks
                    where project_id=$1 and state not in ('merged','cancelled','done','failed')
                    """,
                    PROJECT_ID,
                )
            }
        candidates = {row["code"] for row in rows} | {
            path.name for path in WORKTREE_ROOT.iterdir() if path.is_dir()
        }
        removable = sorted(candidates - active)
        print(f"terminal_tasks={len(rows)} candidate_dirs={len(removable)}")
        if not apply:
            print("dry run; pass --apply to remove worktrees")
            return
        total = sum(cleanup_task_worktrees(code) for code in removable)
        print(f"removed_worktrees={total}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
