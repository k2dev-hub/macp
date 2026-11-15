"""Automatic merge queue consumer.

L1/L2 accepted tasks are merged automatically.  L3 tasks wait for a human
``merge`` approval.  Conflicts and failed post-merge verification create a new
attempt assigned to the previous author so the change is rebased on new main.
"""

from __future__ import annotations

import asyncio
import logging
import os

from .common import heartbeat, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("macp.merge")
PREFIXES = tuple(
    p.strip() for p in os.getenv("MACP_MERGE_PREFIXES", "").split(",") if p.strip()
)
SKIPPED: set[str] = set()


async def approved_for(code: str, project: str) -> bool:
    status, approvals = await asyncio.to_thread(
        request, "GET", "/v1/approvals?pending=false", None, None, project
    )
    if status != 200:
        return False
    return any(
        a.get("task") == code
        and a.get("action") == "merge"
        and a.get("decision") == "approved"
        for a in approvals
    )


async def handle(task: dict, project: str) -> None:
    code = task["code"]
    if PREFIXES and not code.startswith(PREFIXES):
        return
    status_d, detail = await asyncio.to_thread(request, "GET", f"/v1/tasks/{code}", None, None, project)
    if status_d != 200:
        return
    if not detail.get("receipt"):
        return
    if not detail.get("work_order"):
        return
    risk = str(((detail.get("receipt") or {}).get("body") or {}).get("risk_level", "L1"))
    if risk == "L3" and not await approved_for(code, project):
        if code not in SKIPPED:
            log.info("waiting for human merge approval: %s", code)
            SKIPPED.add(code)
        return
    SKIPPED.discard(code)
    status_e, queued = await asyncio.to_thread(
        request, "POST", "/v1/merges", {"task": code}, None, project
    )
    if status_e not in (200, 201):
        if code not in SKIPPED:
            log.warning("enqueue %s failed: %s %s", code, status_e, queued)
            SKIPPED.add(code)
        return
    merge_id = queued.get("merge_id")
    if not merge_id:
        return
    status_p, result = await asyncio.to_thread(
        request, "POST", f"/v1/merges/{merge_id}/process", None, None, project
    )
    state = (result or {}).get("state") if isinstance(result, dict) else None
    log.info("merge %s %s -> %s %s", code, merge_id, status_p, state or result)
    if status_p == 409 and isinstance(result, dict):
        detail_body = result.get("detail") or {}
        if isinstance(detail_body, dict) and detail_body.get("code") == "approval_required":
            log.info("approval required for %s", code)
            SKIPPED.add(code)
            return
    if state in ("needs_resolution", "reverted", "failed"):
        status_r, body = await asyncio.to_thread(
            request,
            "POST",
            f"/v1/tasks/{code}/rework",
            {"reason": f"automatic rework after merge state {state}"},
            None,
            project,
        )
        if status_r in (200, 201):
            log.info("rework %s -> attempt %s", code, body.get("attempt"))
        else:
            log.warning("rework %s failed: %s %s", code, status_r, body)


async def main() -> None:
    log.info("merge loop started prefixes=%s", PREFIXES or "all")
    while True:
        heartbeat()
        status_p, projects = await asyncio.to_thread(request, "GET", "/v1/projects")
        if status_p == 200:
            for project in projects:
                status, tasks = await asyncio.to_thread(
                    request, "GET", "/v1/tasks", None, None, project["code"]
                )
                if status != 200:
                    continue
                for task in tasks:
                    if task["state"] == "accepted":
                        try:
                            await handle(task, project["code"])
                        except Exception:
                            log.exception("merge handler failed for %s", task["code"])
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
