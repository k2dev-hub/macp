"""Reference coordinator: accepts receipts submitted by HTTP workers."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .common import heartbeat, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("macp.coordinator")
TRANSIENT_MARKERS = (
    "connection refused",
    "http 0",
    "timed out",
    "reset by peer",
    "model loop ended without a receipt",
    "missing gate evidence or candidate ref",
    "receipt already exists",
    "task is merged",
    "bubblewrap failed",
    "resource temporarily unavailable",
)
MAX_AUTO_RECOVERIES = 3


def event_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def recover_transient(tasks, project: str) -> None:
    for task in tasks:
        if task["state"] != "blocked":
            continue
        status, detail = await asyncio.to_thread(
            request, "GET", f"/v1/tasks/{task['code']}", None, None, project
        )
        if status != 200:
            continue
        events = detail.get("events") or []
        last_block = next((e for e in events if e["type"] == "task.blocked"), None)
        if not last_block:
            continue
        last_requeue = next((e for e in events if e["type"] == "task.requeued"), None)
        if last_requeue and last_requeue["seq"] > last_block["seq"]:
            continue
        reason = str((last_block.get("payload") or {}).get("reason") or "")
        if not any(marker in reason.lower() for marker in TRANSIENT_MARKERS):
            continue
        if (datetime.now(timezone.utc) - event_time(last_block["occurred_at"])).total_seconds() < 60:
            continue
        recoveries = sum(
            1
            for event in events
            if event["type"] in ("task.requeued", "task.reworked")
            and str((event.get("payload") or {}).get("reason", "")).startswith("auto-recovery")
        )
        if recoveries >= MAX_AUTO_RECOVERIES:
            log.warning("task %s exhausted auto-recovery", task["code"])
            continue
        if "task is merged" in reason.lower():
            action = "reconcile"
            path = f"/v1/tasks/{task['code']}/reconcile"
            payload = None
        elif "receipt already exists" in reason.lower():
            action = "rework"
            path = f"/v1/tasks/{task['code']}/rework"
            payload = {"reason": f"auto-recovery: {reason[:120]}"}
        else:
            action = "requeue"
            path = f"/v1/tasks/{task['code']}/requeue"
            payload = {"reason": f"auto-recovery: {reason[:120]}"}
        status_r, body = await asyncio.to_thread(request, "POST", path, payload, None, project)
        if status_r == 200:
            log.info("auto-%s %s: %s", action, task["code"], reason[:80])
        else:
            log.warning("auto-%s %s failed: %s %s", action, task["code"], status_r, body)


async def reap_expired(project: str) -> None:
    status, assignments = await asyncio.to_thread(
        request, "GET", "/v1/assignments?state=all&expired=true", None, None, project
    )
    if status != 200:
        return
    for assignment in assignments:
        if not assignment.get("expired"):
            continue
        if assignment["state"] not in ("offered", "claimed", "active", "submitted"):
            continue
        status_e, body = await asyncio.to_thread(
            request,
            "POST",
            f"/v1/assignments/{assignment['id']}/expire",
            None,
            None,
            project,
        )
        if status_e == 200:
            log.info("expired %s for %s", assignment["id"], assignment["task"])
        else:
            log.warning("expire %s failed: %s %s", assignment["id"], status_e, body)


async def process_project(project_code: str) -> None:
    await reap_expired(project_code)
    status, tasks = await asyncio.to_thread(
        request, "GET", "/v1/tasks", None, None, project_code
    )
    if status != 200:
        return
    await recover_transient(tasks, project_code)
    for task in tasks:
        if task["state"] != "review":
            continue
        status_d, detail = await asyncio.to_thread(
            request, "GET", f"/v1/tasks/{task['code']}", None, None, project_code
        )
        if status_d != 200 or not detail.get("work_order"):
            continue
        receipt_id = None
        for event in detail.get("events", []):
            if event["type"] == "receipt.submitted":
                receipt_id = (event.get("payload") or {}).get("receipt_id")
                break
        if not receipt_id:
            continue
        status_r, review = await asyncio.to_thread(
            request,
            "POST",
            f"/v1/tasks/{task['code']}/reviews",
            {
                "receipt_id": receipt_id,
                "decision": "accept",
                "findings": [],
                "reviewer": "coordinator-1",
            },
            None,
            project_code,
        )
        if status_r == 200:
            log.info("[%s] accepted %s receipt %s", project_code, task["code"], receipt_id)
            continue
        detail_body = review.get("detail") if isinstance(review, dict) else None
        code = detail_body.get("code") if isinstance(detail_body, dict) else None
        if status_r == 409 and code in ("missing_gates", "missing_evidence"):
            findings = [f"{code}: {detail_body.get('detail') or detail_body}"]
            status_j, rejected = await asyncio.to_thread(
                request,
                "POST",
                f"/v1/tasks/{task['code']}/reviews",
                {
                    "receipt_id": receipt_id,
                    "decision": "reject",
                    "findings": findings,
                    "reviewer": "coordinator-1",
                },
                None,
                project_code,
            )
            if status_j == 200:
                log.info("[%s] auto-rework %s: %s", project_code, task["code"], findings[0])
            else:
                log.warning("[%s] auto-reject %s failed: %s %s", project_code, task["code"], status_j, rejected)
            continue
        log.warning("[%s] review %s receipt %s failed: %s %s", project_code, task["code"], receipt_id, status_r, review)


async def main() -> None:
    log.info("coordinator started")
    while True:
        heartbeat()
        status, projects = await asyncio.to_thread(request, "GET", "/v1/projects")
        if status == 200:
            for project in projects:
                try:
                    await process_project(project["code"])
                except Exception:
                    log.exception("coordinator failed for %s", project["code"])
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
