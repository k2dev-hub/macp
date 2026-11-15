"""Reference scheduler: creates and dispatches tasks for every project."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time

from .common import heartbeat, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("macp.scheduler")

WORKERS = [w.strip() for w in os.getenv("MACP_WORKERS", "worker-e,worker-f,worker-g").split(",") if w.strip()]
MAX_AGENT_TASKS = 30
MAX_HISTORY = int(os.getenv("MACP_SCHEDULER_MAX_HISTORY", "1000"))
MIN_CREATE_INTERVAL = int(os.getenv("MACP_SCHEDULER_CREATE_INTERVAL", "30"))
CREATE_TASKS = os.getenv("MACP_SCHEDULER_CREATE", "0") == "1"
TITLES = [
    "HTTP agent: session expiry",
    "HTTP agent: evidence viewer",
    "HTTP agent: lease conflict",
    "HTTP agent: approval inbox",
    "HTTP agent: gate evidence",
    "HTTP agent: event dedupe",
]
STREAMS = ["stream-core", "stream-ui", "stream-infra"]


def new_code(tasks) -> str:
    used = {task["code"] for task in tasks}
    while True:
        code = f"T-8{random.randint(1000, 9999)}"
        if code not in used:
            return code


async def run_project(project: str, counter: int, last_create: float) -> float:
    status, tasks = await asyncio.to_thread(
        request, "GET", "/v1/tasks", None, None, project
    )
    status_a, agents = await asyncio.to_thread(
        request, "GET", "/v1/agents", None, None, project
    )
    if status != 200 or status_a != 200:
        return last_create
    history = [task for task in tasks if task["code"].startswith("T-8")]
    agent_tasks = [
        task
        for task in history
        if task["state"] not in ("cancelled", "done", "merged")
    ]
    ready = [
        task
        for task in tasks
        if task["state"] == "ready"
        and not task["code"].startswith("T-9")
        # Hard dependencies: the API rejects dispatch too, but filtering here
        # keeps the journal free of one warning per task per scheduler tick.
        and task.get("dependencies_met", True)
    ]
    idle = [
        agent
        for agent in agents
        if agent["code"] in WORKERS and agent["status"] == "idle" and not agent["current_task"]
    ]
    if (
        CREATE_TASKS
        and len(history) < MAX_HISTORY
        and len(agent_tasks) < MAX_AGENT_TASKS
        and len(ready) < 2
        and idle
        and time.monotonic() - last_create >= MIN_CREATE_INTERVAL
    ):
        code = new_code(tasks)
        status_c, created = await asyncio.to_thread(
            request,
            "POST",
            "/v1/tasks",
            {
                "code": code,
                "title": random.choice(TITLES),
                "workstream": STREAMS[counter % len(STREAMS)],
                "kind": random.choice(["slice", "fix", "infra"]),
                "priority": random.choice(["P1", "P2"]),
            },
            None,
            project,
        )
        if status_c == 201:
            log.info("[%s] created %s", project, code)
            ready.append({**created, "code": code, "state": "ready"})
        last_create = time.monotonic()
    elif not CREATE_TASKS:
        pass
    elif len(history) >= MAX_HISTORY:
        return last_create
    elif len(agent_tasks) >= MAX_AGENT_TASKS and len(ready) < 2:
        accepted = [task for task in agent_tasks if task["state"] == "accepted"]
        if accepted:
            oldest = min(accepted, key=lambda task: task["updated_at"])
            status_r, _ = await asyncio.to_thread(
                request, "POST", f"/v1/tasks/{oldest['code']}/recycle", None, None, project
            )
            if status_r == 200:
                log.info("[%s] recycled %s", project, oldest["code"])
                ready.append({"code": oldest["code"], "state": "ready"})

    idle_codes = [agent["code"] for agent in idle]
    random.shuffle(idle_codes)
    for task in ready:
        if not idle_codes:
            break
        worker = idle_codes.pop(0)
        status_d, dispatched = await asyncio.to_thread(
            request,
            "POST",
            f"/v1/tasks/{task['code']}/dispatch",
            {"agent": worker},
            None,
            project,
        )
        if status_d == 201:
            log.info("[%s] dispatched %s -> %s", project, task["code"], worker)
        else:
            log.warning(
                "[%s] dispatch %s -> %s failed: %s %s",
                project,
                task["code"],
                worker,
                status_d,
                dispatched,
            )
    return last_create


async def main() -> None:
    log.info("scheduler started for %s (create_tasks=%s)", WORKERS, CREATE_TASKS)
    counter = 0
    last_create = 0.0
    while True:
        heartbeat()
        status, projects = await asyncio.to_thread(request, "GET", "/v1/projects")
        if status == 200:
            for project in projects:
                counter += 1
                try:
                    last_create = await run_project(project["code"], counter, last_create)
                except Exception:
                    log.exception("scheduler failed for %s", project["code"])
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
