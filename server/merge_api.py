"""Single-lane merge queue with rebase, gate verification and revert."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from .db import current_project, emit_event
from .tool_registry import (
    repo_path,
    worktree_root,
    ensure_repo,
    git,
    require_write_token,
    run_command,
    security_scan,
    cleanup_task_worktrees,
)
from .workorders import LEVEL_NAMES, effective_level, required_gates

router = APIRouter(
    prefix="/v1/merges", tags=["merge"], dependencies=[Depends(require_write_token)]
)


def pool(request: Request):
    return request.app.state.pool


def gate_command(gate: str) -> list[str]:
    if gate == "compile":
        return [sys.executable, "-m", "compileall", "-q", "."]
    if gate == "unit":
        return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    if gate == "integration":
        return [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests/integration",
            "-v",
        ]
    # Platform gates: same fixed, server-side allowlisted argv the tool gateway
    # uses.  Keep the two in sync 閳?an unsupported gate here used to turn a merge
    # into a 500 for any task whose acceptance included `build`/`package`.
    if gate == "build":
        return ["make"]
    if gate == "package":
        return ["make", "package"]
    if gate == "android-build":
        return ["gradle", "--offline", "--no-daemon", "assembleDebug"]
    if gate == "android-test":
        return ["gradle", "--offline", "--no-daemon", "testDebugUnitTest"]
    if gate == "windows-build":
        return ["cmd", "/c", "build.bat"]
    raise ValueError(f"unsupported gate: {gate}")


def diff_stats(base: str, candidate: str) -> tuple[list[str], int]:
    try:
        numstat = git("diff", "--numstat", f"{base}..{candidate}", cwd=repo_path()).stdout
    except Exception:
        return [], 0
    paths, lines = [], 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        paths.append(path)
        lines += int(added) if added.isdigit() else 0
        lines += int(deleted) if deleted.isdigit() else 0
    return paths, lines


def resolve_base(base: str, candidate: str) -> str:
    """Return a usable commit for the merge worktree."""

    if base and base != "demo-base":
        check = git("rev-parse", "--verify", base, cwd=repo_path(), check=False)
        if check.returncode == 0:
            return base
    return git("merge-base", "main", candidate, cwd=repo_path()).stdout.strip()


def prepare_merge(base: str, candidate: str, merge_id: uuid.UUID) -> tuple[Path | None, str | None]:
    ensure_repo()
    try:
        base = resolve_base(base, candidate)
    except Exception as exc:
        return None, f"cannot resolve merge base: {exc}"
    path = worktree_root().parent / "merge" / str(merge_id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git("worktree", "prune", cwd=repo_path())
    try:
        git("worktree", "add", "--detach", str(path), base, cwd=repo_path())
        git("checkout", "-B", f"merge/{merge_id}", candidate, cwd=path)
        rebase = git(
            "-c",
            "user.name=MACP Merge",
            "-c",
            "user.email=merge@macp.local",
            "rebase",
            "main",
            cwd=path,
            check=False,
        )
    except Exception as exc:
        return None, str(exc)
    if rebase.returncode != 0:
        git("rebase", "--abort", cwd=path, check=False)
        return None, (rebase.stderr or rebase.stdout or "rebase conflict").strip()
    return path, None


def cleanup(path: Path) -> None:
    git("worktree", "remove", "--force", str(path), cwd=repo_path(), check=False)


@router.post("", status_code=201)
async def enqueue(body: dict, request: Request):
    code = body.get("task")
    if not code:
        raise HTTPException(422, "task is required")
    async with pool(request).acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                "select * from tasks where project_id=$1 and code=$2 for update",
                current_project().id,
                code,
            )
            if not task:
                raise HTTPException(404, "task not found")
            if task["state"] != "accepted":
                raise HTTPException(409, f"task is {task['state']}, expected accepted")
            attempt = task["current_attempt"] or 1
            candidate_ref = f"refs/candidates/{code}/{attempt}"
            exists = git("rev-parse", "--verify", candidate_ref, cwd=repo_path(), check=False)
            if exists.returncode != 0:
                raise HTTPException(409, f"candidate ref not found: {candidate_ref}")
            attempt_row = await conn.fetchrow(
                "select id, base_commit from attempts where task_id=$1 and number=$2",
                task["id"],
                attempt,
            )
            if attempt_row:
                attempt_id = attempt_row["id"]
                base = attempt_row["base_commit"] or git(
                    "rev-parse", "main", cwd=repo_path()
                ).stdout.strip()
            else:
                attempt_id = uuid.uuid4()
                base = git("rev-parse", "main", cwd=repo_path()).stdout.strip()
                await conn.execute(
                    """
                    insert into attempts (id, task_id, number, state, base_commit, branch, candidate_ref)
                    values ($1,$2,$3,'submitted',$4,$5,$6)
                    """,
                    attempt_id,
                    task["id"],
                    attempt,
                    base,
                    f"work/{code}/{attempt}",
                    candidate_ref,
                )
            active = await conn.fetchrow(
                """
                select id, state, candidate_ref from merge_requests
                where task_id=$1 and state in ('queued','verifying','merging','conflict','needs_resolution')
                """,
                task["id"],
            )
            if active:
                if active["state"] in ("conflict", "needs_resolution"):
                    await conn.execute(
                        """
                        update merge_requests
                        set state='queued', attempt_id=$1, candidate_ref=$2,
                            base_commit=$3, merge_commit=null, verification_run_ids='[]'::jsonb,
                            updated_at=now()
                        where id=$4
                        """,
                        attempt_id,
                        candidate_ref,
                        base,
                        active["id"],
                    )
                    await emit_event(
                        conn, "merge.requeued", "merge", active["id"], 1, "service", None,
                        code, {"candidate_ref": candidate_ref, "attempt": attempt},
                    )
                return {"merge_id": str(active["id"]), "state": "queued", "reused": True}
            merge_id = uuid.uuid4()
            await conn.execute(
                """
                insert into merge_requests (
                  id, project_id, task_id, attempt_id, candidate_ref, target_ref,
                  base_commit, state
                ) values ($1,$2,$3,$4,$5,'refs/heads/main',$6,'queued')
                """,
                merge_id,
                current_project().id,
                task["id"],
                attempt_id,
                candidate_ref,
                base,
            )
            await emit_event(
                conn, "merge.queued", "merge", merge_id, 1, "agent", None, code,
                {"candidate_ref": candidate_ref, "base_commit": base},
            )
    return {"merge_id": str(merge_id), "state": "queued", "candidate_ref": candidate_ref}


@router.get("/next")
async def next_merge(request: Request):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            select m.id, m.candidate_ref, m.base_commit, t.code as task
            from merge_requests m join tasks t on t.id=m.task_id
            where m.state='queued' order by m.created_at limit 1
            """
        )
    if not row:
        return {"merge": None}
    return {
        "merge_id": str(row["id"]),
        "task": row["task"],
        "candidate_ref": row["candidate_ref"],
        "base_commit": row["base_commit"],
    }


PROCESSING: set[str] = set()


@router.post("/{merge_id}/process")
async def process(merge_id: uuid.UUID, request: Request):
    key = str(merge_id)
    if key in PROCESSING:
        raise HTTPException(
            409, {"code": "merge_busy", "detail": "merge is already being processed"}
        )
    PROCESSING.add(key)
    lock_name = f"merge:{merge_id}"
    try:
        async with request.app.state.pool.acquire() as lock_conn:
            got = await lock_conn.fetchval(
                "select pg_try_advisory_lock(hashtext($1))", lock_name
            )
            if not got:
                raise HTTPException(
                    409,
                    {"code": "merge_busy", "detail": "merge is already being processed"},
                )
            try:
                return await _process(merge_id, request)
            finally:
                await lock_conn.execute(
                    "select pg_advisory_unlock(hashtext($1))", lock_name
                )
    finally:
        PROCESSING.discard(key)


async def _process(merge_id: uuid.UUID, request: Request):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            select m.*, t.code as task_code, t.current_attempt,
                   t.version as task_version, t.active_work_order_id
            from merge_requests m
            join tasks t on t.id=m.task_id
            where m.id=$1 for update
            """,
            merge_id,
        )
        if not row:
            raise HTTPException(404, "merge not found")
        if row["state"] not in ("queued", "verifying", "conflict", "needs_resolution"):
            raise HTTPException(409, f"merge is {row['state']}")
        row = dict(row)
        # A rework may have produced a newer attempt while this merge request was
        # waiting in needs_resolution.  Always process the current attempt.
        current_candidate = (
            f"refs/candidates/{row['task_code']}/{row['current_attempt'] or 1}"
        )
        if row["candidate_ref"] != current_candidate:
            check = git("rev-parse", "--verify", current_candidate, cwd=repo_path(), check=False)
            attempt_row = await conn.fetchrow(
                "select id, base_commit from attempts where task_id=$1 and number=$2",
                row["task_id"],
                row["current_attempt"] or 1,
            )
            if check.returncode == 0 and attempt_row:
                await conn.execute(
                    """
                    update merge_requests
                    set candidate_ref=$1, base_commit=$2, attempt_id=$3,
                        state='queued', merge_commit=null
                    where id=$4
                    """,
                    current_candidate,
                    attempt_row["base_commit"],
                    attempt_row["id"],
                    merge_id,
                )
                await emit_event(
                    conn, "merge.requeued", "merge", merge_id, 1, "service", None,
                    row["task_code"],
                    {"candidate_ref": current_candidate, "attempt": row["current_attempt"]},
                )
                row["candidate_ref"] = current_candidate
                row["base_commit"] = attempt_row["base_commit"]
                row["attempt_id"] = attempt_row["id"]
                row["state"] = "queued"
        work_order_row = None
        if row["active_work_order_id"]:
            work_order_row = await conn.fetchrow(
                "select body from work_orders where id=$1", row["active_work_order_id"]
            )
        work_order = (work_order_row["body"] if work_order_row else {}) or {}
        changed_paths, changed_lines = await asyncio.to_thread(
            diff_stats, row["base_commit"], row["candidate_ref"]
        )
        level = effective_level(work_order, changed_paths, changed_lines)
        if level >= 3:
            approval = await conn.fetchrow(
                """
                select id from approvals
                where project_id=$1 and task_id=$2 and action='merge'
                  and decision='approved'
                order by decided_at desc limit 1
                """,
                current_project().id,
                row["task_id"],
            )
            if not approval:
                await emit_event(
                    conn,
                    "merge.approval_required",
                    "merge",
                    merge_id,
                    1,
                    "service",
                    None,
                    row["task_code"],
                    {
                        "risk": LEVEL_NAMES.get(level, "L3"),
                        "changed_paths": changed_paths[:20],
                    },
                )
                raise HTTPException(
                    409,
                    {
                        "code": "approval_required",
                        "detail": "L3 change requires an approved merge approval",
                        "risk": LEVEL_NAMES.get(level, "L3"),
                    },
                )
        gates = required_gates(work_order, changed_paths, changed_lines) or ["compile"]
        await conn.execute("update merge_requests set state='verifying' where id=$1", merge_id)

    context = f"merge/{merge_id}"
    main_at_start = git("rev-parse", "main", cwd=repo_path()).stdout.strip()
    path, error = await asyncio.to_thread(
        prepare_merge, row["base_commit"], row["candidate_ref"], merge_id
    )
    if error:
        if path is not None:
            await asyncio.to_thread(cleanup, path)
        async with pool(request).acquire() as conn:
            await conn.execute(
                "update merge_requests set state='needs_resolution' where id=$1", merge_id
            )
            await conn.execute(
                "update tasks set state='blocked', version=version+1, updated_at=now() where id=$1",
                row["task_id"],
            )
            await conn.execute(
                "update agents set status='idle', current_task_id=null where current_task_id=$1",
                row["task_id"],
            )
            await emit_event(
                conn, "merge.conflict", "merge", merge_id, 1, "service", None,
                row["task_code"], {"reason": error[:1000]},
            )
        return {"merge_id": str(merge_id), "state": "needs_resolution", "error": error[:1000]}

    assert path is not None
    head = git("rev-parse", "HEAD", cwd=path).stdout.strip()
    gate_results = []
    for gate in gates:
        if gate == "security":
            scan = await asyncio.to_thread(security_scan, path)
            result = {
                "exit": scan["exit"],
                "stdout": "\n".join(scan["findings"]),
                "stderr": "" if scan["exit"] == 0 else "security findings detected",
            }
        else:
            # Re-run the gate in the environment the task declared; a kvm-debian
            # task must not be re-verified on the local runner, and platform gates
            # (make/package) need far more than 60s.
            profile = str(
                ((work_order or {}).get("execution") or {}).get("profile") or "local"
            )
            gate_timeout = 1800 if gate.startswith(("android-", "windows-")) else (900 if gate in ("build", "package") else 60)
            result = await asyncio.to_thread(
                run_command, gate_command(gate), path, gate_timeout, profile
            )
        log = (
            f"gate={gate}\nexit={result['exit']}\n--- stdout ---\n{result['stdout']}\n"
            f"--- stderr ---\n{result['stderr']}\n"
        ).encode("utf-8")
        digest = hashlib.sha256(log).hexdigest()
        evidence_id = uuid.uuid4()
        log_path = path / ".evidence" / f"merge-{evidence_id}.log"
        log_path.parent.mkdir(exist_ok=True)
        log_path.write_bytes(log)
        async with pool(request).acquire() as conn:
            stored_id = await conn.fetchval(
                """
                insert into evidence (id, project_id, sha256, uri, media_type, size_bytes, kind, signature)
                values ($1,$2,$3,$4,'text/plain',$5,'log','merge-service')
                on conflict (project_id, sha256) do update set uri=excluded.uri,
                  size_bytes=excluded.size_bytes returning id
                """,
                evidence_id,
                current_project().id,
                digest,
                f"file://{log_path}",
                len(log),
            )
        gate_results.append(
            {"gate": gate, "exit": result["exit"], "evidence_id": str(stored_id)}
        )
        if result["exit"] != 0:
            async with pool(request).acquire() as conn:
                await conn.execute(
                    "update merge_requests set state='needs_resolution' where id=$1", merge_id
                )
                await conn.execute(
                    "update tasks set state='blocked', version=version+1, updated_at=now() where id=$1",
                    row["task_id"],
                )
                await conn.execute(
                    "update agents set status='idle', current_task_id=null where current_task_id=$1",
                    row["task_id"],
                )
                await emit_event(
                    conn, "merge.conflict", "merge", merge_id, 1, "service", None,
                    row["task_code"], {"reason": f"gate {gate} failed", "gates": gate_results},
                )
            await asyncio.to_thread(cleanup, path)
            return {"merge_id": str(merge_id), "state": "needs_resolution", "gates": gate_results}

    current_main = git("rev-parse", "main", cwd=repo_path()).stdout.strip()
    if current_main != main_at_start:
        await asyncio.to_thread(cleanup, path)
        async with pool(request).acquire() as conn:
            await conn.execute(
                "update merge_requests set state='queued' where id=$1", merge_id
            )
            await emit_event(
                conn, "merge.requeued", "merge", merge_id, 1, "service", None,
                row["task_code"], {"reason": "main moved during verification"},
            )
        return {"merge_id": str(merge_id), "state": "queued", "reason": "main moved"}

    git("update-ref", "refs/heads/main", head, cwd=repo_path())
    async with pool(request).acquire() as conn:
        await conn.execute(
            """
            update merge_requests
            set state='merged', merge_commit=$1, updated_at=now()
            where id=$2
            """,
            head,
            merge_id,
        )
        version = await conn.fetchval(
            "update tasks set state='merged', version=version+1, merged_at=now(), updated_at=now() where id=$1 returning version",
            row["task_id"],
        )
        await emit_event(
            conn, "merge.merged", "merge", merge_id, 1, "service", None,
            row["task_code"], {"merge_commit": head, "gates": gate_results},
        )
        await emit_event(
            conn, "push.done", "repo", merge_id, 1, "service", None,
            row["task_code"], {"ref": "refs/heads/main", "commit": head},
        )
        await conn.execute(
            "update leases set state='released', released_at=now() where owner_task_id=$1 and state='active'",
            row["task_id"],
        )
        await conn.execute(
            """
            update agents set status='idle', current_task_id=null
            where current_task_id=$1
            """,
            row["task_id"],
        )
        await emit_event(
            conn, "task.merged", "task", row["task_id"], version, "service", None,
            row["task_code"], {"merge_commit": head},
        )

    # Post-merge verification; on failure, produce a revert commit and reopen.
    post_failed = False
    for gate in gates:
        if gate == "security":
            scan = await asyncio.to_thread(security_scan, path)
            failed = scan["exit"] != 0
        else:
            profile = str(
                ((work_order or {}).get("execution") or {}).get("profile") or "local"
            )
            gate_timeout = 1800 if gate.startswith(("android-", "windows-")) else (900 if gate in ("build", "package") else 60)
            result = await asyncio.to_thread(
                run_command, gate_command(gate), path, gate_timeout, profile
            )
            failed = result["exit"] != 0
        if failed:
            post_failed = True
            break
    if post_failed:
        revert = git(
            "-c", "user.name=MACP Merge", "-c", "user.email=merge@macp.local",
            "revert", "--no-edit", "HEAD", cwd=path, check=False,
        )
        if revert.returncode == 0:
            revert_head = git("rev-parse", "HEAD", cwd=path).stdout.strip()
            git("update-ref", "refs/heads/main", revert_head, cwd=repo_path())
            async with pool(request).acquire() as conn:
                await conn.execute(
                    "update merge_requests set state='reverted', updated_at=now() where id=$1",
                    merge_id,
                )
                version = await conn.fetchval(
                    "update tasks set state='reopened', version=version+1, updated_at=now() where id=$1 returning version",
                    row["task_id"],
                )
                await emit_event(
                    conn, "merge.reverted", "merge", merge_id, 1, "service", None,
                    row["task_code"], {"revert_commit": revert_head},
                )
                await conn.execute(
                    "update leases set state='released', released_at=now() where owner_task_id=$1 and state='active'",
                    row["task_id"],
                )
                await emit_event(
                    conn, "task.reopened", "task", row["task_id"], version, "service", None,
                    row["task_code"], {"reason": "post-merge verification failed"},
                )
        else:
            async with pool(request).acquire() as conn:
                await conn.execute(
                    "update merge_requests set state='failed' where id=$1", merge_id
                )
                await emit_event(
                    conn, "merge.failed", "merge", merge_id, 1, "service", None,
                    row["task_code"], {"reason": "post-merge verify failed; revert failed"},
                )
        await asyncio.to_thread(cleanup, path)
        return {"merge_id": str(merge_id), "state": "reverted" if revert.returncode == 0 else "failed"}

    await asyncio.to_thread(cleanup, path)
    await asyncio.to_thread(cleanup_task_worktrees, row["task_code"])
    return {
        "merge_id": str(merge_id),
        "state": "merged",
        "merge_commit": head,
        "gates": gate_results,
    }
