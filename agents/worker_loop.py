"""Reference worker: claims an assignment, makes a real change and runs gates.

It does not use a language model.  It exercises exactly the same tool gateway
that a bare LLM uses, so its receipts carry real gate evidence and candidate
refs.  Useful as a deterministic worker pool and as a protocol probe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from .common import heartbeat, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("macp.worker")


async def call_tool(token: str, task: str, agent: str, attempt: int, name: str, args: dict):
    heartbeat()
    status, result = await asyncio.to_thread(
        request,
        "POST",
        f"/v1/tools/{name}/invoke",
        {
            "tool_call_id": f"tc_{name}_{agent}",
            "agent": agent,
            "task": task,
            "attempt": attempt,
            "arguments": args,
        },
        token,
    )
    if status != 200:
        raise RuntimeError(f"{name}: HTTP {status} {result}")
    if isinstance(result, dict) and result.get("status") == "error":
        raise RuntimeError(f"{name}: {(result.get('error') or {}).get('message', result)}")
    return (result or {}).get("output") or {}


async def block(token: str, agent: str, task: str, reason: str) -> None:
    await asyncio.to_thread(
        request,
        "POST",
        f"/v1/agents/{agent}/events",
        {
            "type": "task.blocked",
            "payload": {
                "task": task,
                "reason": reason,
                "options": ["reassign", "reduce scope", "handoff"],
            },
        },
        token,
    )


async def process_project(project: str, name: str) -> bool:
    status, assignment = await asyncio.to_thread(
        request, "GET", f"/v1/assignments/next?agent={name}", None, None, project
    )
    if status != 200 or not assignment:
        return False
    heartbeat()
    assignment_id = assignment["assignment_id"]
    task = assignment["task"]
    log.info("[%s] %s picked %s (%s)", project, name, task, assignment_id)
    status, claim = await asyncio.to_thread(
        request, "POST", f"/v1/assignments/{assignment_id}/claim", None, None, project
    )
    if status != 200:
        log.warning("%s claim failed: %s %s", name, status, claim)
        return False
    token = claim["token"]["value"]
    attempt = int(claim.get("attempt") or 1)
    try:
        await asyncio.to_thread(
            request, "POST", f"/v1/assignments/{assignment_id}/heartbeat", None, token
        )
        await asyncio.to_thread(
            request,
            "POST",
            f"/v1/agents/{name}/events",
            {"type": "turn.started", "payload": {"task": task, "attempt": attempt}},
            token,
        )
        content = (
            "# task " + task + " attempt " + str(attempt) + " agent " + name + chr(10)
            + "def add(a, b):" + chr(10)
            + "    return a + b" + chr(10)
        )
        path = "src/task_" + task.replace("-", "_") + ".py"
        await call_tool(
            token, task, name, attempt, "workspace_write_file",
            {"path": path, "content": content},
        )
        gate = await call_tool(token, task, name, attempt, "gate_run", {"gate": "compile"})
        if gate.get("exit") != 0:
            await block(
                token, name, task,
                f"compile gate failed: {gate.get('stderr', '')[:200]}",
            )
            return True
        await call_tool(
            token, task, name, attempt, "workspace_commit",
            {"message": f"{task}: deterministic worker change (attempt {attempt})"},
        )
        submit = await call_tool(token, task, name, attempt, "workspace_submit", {})
        receipt = await call_tool(
            token, task, name, attempt, "receipt_submit",
            {
                "receipt_id": f"R-{abs(hash((task, attempt, name))) % 10**9:09d}",
                "candidate_ref": submit["candidate_ref"],
                "base_commit": submit["base_commit"],
                "gates": [
                    {
                        "gate": "compile",
                        "exit": gate["exit"],
                        "evidence_id": gate["evidence_id"],
                    }
                ],
            },
        )
        log.info("%s submitted %s for %s", name, receipt.get("receipt_id"), task)
        await asyncio.to_thread(
            request,
            "POST",
            f"/v1/agents/{name}/events",
            {"type": "turn.ended", "payload": {"task": task, "attempt": attempt}},
            token,
        )
    except Exception as exc:
        log.warning("[%s] %s task %s failed: %s", project, name, task, exc)
        try:
            await block(token, name, task, str(exc)[:300])
        except Exception:
            pass
    return True


async def worker(name: str) -> None:
    log.info("worker %s started", name)
    while True:
        heartbeat()
        status, projects = await asyncio.to_thread(request, "GET", "/v1/projects")
        processed = False
        if status == 200:
            for project in projects:
                try:
                    processed = await process_project(project["code"], name) or processed
                except Exception:
                    log.exception("[%s] worker failed", project["code"])
        await asyncio.sleep(2 if processed else 3)


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else os.getenv("MACP_WORKER", "worker-e")
    asyncio.run(worker(name))


if __name__ == "__main__":
    main()
