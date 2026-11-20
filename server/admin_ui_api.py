"""Console-facing endpoints that the six management pages need.

The console could already *observe* the platform; these endpoints let an
administrator *administer* it: projects, artifacts, chain state, agents, how
work is divided, and which model each agent uses.

Everything here is read-mostly and guarded by the write token; the two write
paths (project rename, agent settings) are narrow on purpose.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .auth import require_write_token
from .db import current_project

router = APIRouter(
    prefix="/v1/ui",
    tags=["ui"],
    dependencies=[Depends(require_write_token)],
)

WORKSPACE_ROOT = Path(os.getenv("MACP_WORKSPACE_ROOT", "/app/workspace")).resolve()

ARTIFACT_KINDS = ("artifact", "report", "manifest")


def _pool(request: Request):
    return request.app.state.pool


def _safe_local_path(uri: str) -> Path | None:
    """Turn a stored `file://…` uri into a path inside the workspace, or None.

    Evidence rows point at files the gate wrote; anything outside the workspace
    (or a non-file scheme) is refused rather than served.
    """

    if not uri or not uri.startswith("file://"):
        return None
    raw = Path(uri[len("file://"):])
    try:
        resolved = raw.resolve()
    except OSError:
        return None
    if WORKSPACE_ROOT not in resolved.parents and resolved != WORKSPACE_ROOT:
        return None
    return resolved if resolved.is_file() else None


# --------------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------------- #


@router.get("/projects")
async def list_projects(request: Request):
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select p.code, p.name, p.policy, p.created_at, p.updated_at,
                   (select count(*) from tasks where project_id = p.id) as tasks,
                   (select count(*) from agents where project_id = p.id) as agents
            from projects p
            order by p.created_at
            """
        )
    return [
        {
            "code": row["code"],
            "name": row["name"],
            "policy": row["policy"],
            "tasks": row["tasks"],
            "agents": row["agents"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "current": row["code"] == current_project().code,
        }
        for row in rows
    ]


@router.patch("/projects/{code}")
async def update_project(code: str, request: Request, body: dict = Body(...)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    async with _pool(request).acquire() as conn:
        updated = await conn.fetchval(
            """
            update projects set name=$2, updated_at=now(), version=version+1
            where code=$1 returning version
            """,
            code,
            name,
        )
    if not updated:
        raise HTTPException(404, "project not found")
    return {"code": code, "name": name, "version": updated}


# --------------------------------------------------------------------------- #
# artifacts: what the gates produced, and what can be downloaded
# --------------------------------------------------------------------------- #


@router.get("/artifacts")
async def list_artifacts(request: Request, limit: int = Query(default=40, ge=1, le=200)):
    project = current_project()
    async with _pool(request).acquire() as conn:
        evidence = await conn.fetch(
            """
            select e.id, e.kind, e.media_type, e.size_bytes, e.sha256, e.uri, e.created_at,
                   t.code as task, g.code as gate, gr.exit_code
            from evidence e
            left join gate_runs gr on gr.evidence_id = e.id
            left join tasks t on t.id = gr.task_id
            left join gate_defs g on g.id = gr.gate_def_id
            where e.project_id=$1
            order by e.created_at desc
            limit $2
            """,
            project.id,
            limit,
        )
        builds = await conn.fetch(
            """
            select build_id, task_code, gate, artifact_name, artifact_sha256,
                   artifact_size, artifact_path, fabric_status, fabric_tx_id, created_at
            from build_provenance
            where project_id=$1
            order by created_at desc
            limit $2
            """,
            project.id,
            limit,
        )
    return {
        "evidence": [
            {
                "id": str(row["id"]),
                "kind": row["kind"],
                "media_type": row["media_type"],
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
                "task": row["task"],
                "gate": row["gate"],
                "exit_code": row["exit_code"],
                "created_at": row["created_at"].isoformat(),
                "downloadable": _safe_local_path(row["uri"]) is not None,
            }
            for row in evidence
        ],
        "builds": [
            {
                "build_id": row["build_id"],
                "task": row["task_code"],
                "gate": row["gate"],
                "artifact_name": row["artifact_name"],
                "artifact_sha256": row["artifact_sha256"],
                "artifact_size": row["artifact_size"],
                "fabric_status": row["fabric_status"],
                "fabric_tx_id": row["fabric_tx_id"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in builds
        ],
    }


@router.get("/artifacts/{evidence_id}/download")
async def download_evidence(request: Request, evidence_id: str):
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            "select uri, media_type, sha256 from evidence where id=$1 and project_id=$2",
            evidence_id,
            current_project().id,
        )
    if row is None:
        raise HTTPException(404, "evidence not found")
    path = _safe_local_path(row["uri"])
    if path is None:
        raise HTTPException(410, "the file is no longer on disk")
    return FileResponse(path, media_type=row["media_type"] or "application/octet-stream",
                        filename=f"{row['sha256'][:12]}-{path.name}")


# --------------------------------------------------------------------------- #
# agents: settings, and who is doing what
# --------------------------------------------------------------------------- #


@router.get("/agents")
async def list_ui_agents(request: Request):
    project = current_project()
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select a.code, a.role, a.status, a.model_policy, a.capabilities,
                   a.allowed_paths, a.heartbeat_at, t.code as current_task,
                   (select name from llm_providers p where p.agent_code = a.code
                     and p.project_id = a.project_id order by p.updated_at desc limit 1) as provider,
                   (select model from llm_providers p where p.agent_code = a.code
                     and p.project_id = a.project_id order by p.updated_at desc limit 1) as provider_model
            from agents a
            left join tasks t on t.id = a.current_task_id
            where a.project_id=$1
            order by case a.role when 'coordinator' then 0 when 'observer' then 1
                                  when 'worker' then 2 else 3 end, a.code
            """,
            project.id,
        )
        providers = await conn.fetch(
            "select id, agent_code, name, model, base_url, enabled from llm_providers "
            "where project_id=$1 order by agent_code",
            project.id,
        )
    return {
        "agents": [
            {
                "code": row["code"],
                "role": row["role"],
                "status": row["status"],
                "model_policy": row["model_policy"] or {},
                "capabilities": row["capabilities"] or [],
                "allowed_paths": row["allowed_paths"] or [],
                "current_task": row["current_task"],
                "heartbeat_at": row["heartbeat_at"].isoformat() if row["heartbeat_at"] else None,
                "provider": row["provider"],
                "provider_model": row["provider_model"],
            }
            for row in rows
        ],
        "providers": [
            {
                "id": str(row["id"]),
                "agent_code": row["agent_code"],
                "name": row["name"],
                "model": row["model"],
                "base_url": row["base_url"],
                "enabled": row["enabled"],
            }
            for row in providers
        ],
    }


@router.patch("/agents/{code}")
async def update_agent(code: str, request: Request, body: dict = Body(...)):
    """Update the narrow, safe parts of an agent: capabilities, allowed paths,
    model policy (provider/model/temperature), and status."""

    project = current_project()
    fields: list[str] = []
    values: list[Any] = []

    if "capabilities" in body:
        capabilities = body["capabilities"]
        if not isinstance(capabilities, list):
            raise HTTPException(422, "capabilities must be a list")
        fields.append("capabilities")
        values.append([str(item) for item in capabilities])
    if "allowed_paths" in body:
        paths = body["allowed_paths"]
        if not isinstance(paths, list):
            raise HTTPException(422, "allowed_paths must be a list")
        fields.append("allowed_paths")
        values.append([str(item) for item in paths])
    if "model_policy" in body:
        policy = body["model_policy"]
        if not isinstance(policy, dict):
            raise HTTPException(422, "model_policy must be an object")
        fields.append("model_policy")
        values.append(json.dumps(policy))
    if "status" in body:
        status = str(body["status"])
        allowed = {"offline", "idle", "working", "waiting_user", "blocked", "stalled", "dead"}
        if status not in allowed:
            raise HTTPException(422, f"status must be one of {sorted(allowed)}")
        fields.append("status")
        values.append(status)

    if not fields:
        raise HTTPException(422, "nothing to update")

    assignments = ", ".join(f"{name} = ${index + 3}" for index, name in enumerate(fields))
    sql = f"update agents set {assignments} where code = $1 and project_id = $2 returning code"
    async with _pool(request).acquire() as conn:
        updated = await conn.fetchval(sql, code, project.id, *values)
    if not updated:
        raise HTTPException(404, "agent not found")
    return {"code": code, "updated": fields}


@router.get("/workload")
async def workload(request: Request):
    """Who is doing what: per agent, the current task and recent state counts."""

    project = current_project()
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            select a.code, a.role, a.status, a.capabilities, a.allowed_paths,
                   t.code as current_task, t.title as current_title, t.state as current_state,
                   (select count(*) from assignments s
                     where s.agent_id = a.id and s.state in ('offered','claimed','active')) as active_assignments,
                   (select count(*) from assignments s
                     where s.agent_id = a.id and s.created_at > now() - interval '24 hours') as assignments_24h,
                   (select count(distinct tk.id) from tasks tk
                     join assignments s2 on s2.task_id = tk.id and s2.agent_id = a.id
                     where tk.state = 'blocked'
                       and tk.updated_at > now() - interval '24 hours') as blocked_24h
            from agents a
            left join tasks t on t.id = a.current_task_id
            where a.project_id=$1
            order by case a.role when 'worker' then 0 else 1 end, a.code
            """,
            project.id,
        )
        by_state = await conn.fetch(
            """
            select state, count(*) as n from tasks where project_id=$1 group by state order by n desc
            """,
            project.id,
        )
        in_flight = await conn.fetch(
            """
            select tk.code, tk.title, tk.state, ag.code as agent, tk.updated_at
            from tasks tk
            left join lateral (
              select a.agent_id from assignments a
              where a.task_id = tk.id and a.state in ('offered','claimed','active','submitted')
              order by a.created_at desc limit 1
            ) a on true
            left join agents ag on ag.id = a.agent_id
            where tk.project_id=$1 and tk.state in ('ready','assigned','in_progress','review','blocked')
            order by tk.updated_at desc limit 12
            """,
            project.id,
        )
    return {
        "agents": [
            {
                "code": row["code"],
                "role": row["role"],
                "status": row["status"],
                "capabilities": row["capabilities"] or [],
                "allowed_paths": row["allowed_paths"] or [],
                "current_task": row["current_task"],
                "current_title": row["current_title"],
                "current_state": row["current_state"],
                "active_assignments": row["active_assignments"],
                "assignments_24h": row["assignments_24h"],
                "blocked_24h": row["blocked_24h"],
            }
            for row in rows
        ],
        "by_state": [{"state": row["state"], "count": row["n"]} for row in by_state],
        "in_flight": [
            {
                "code": row["code"],
                "title": row["title"],
                "state": row["state"],
                "agent": row["agent"],
                "updated_at": row["updated_at"].isoformat(),
            }
            for row in in_flight
        ],
    }


# --------------------------------------------------------------------------- #
# chain: what an administrator needs to see about anchoring
# --------------------------------------------------------------------------- #


@router.get("/chain")
async def chain_overview(request: Request):
    from . import fabric

    project = current_project()
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            select count(*) as builds,
                   count(*) filter (where fabric_status = 'anchored') as anchored,
                   count(*) filter (where fabric_status = 'failed') as failed,
                   count(*) filter (where fabric_status is null) as unanchored,
                   max(fabric_anchored) as last_anchored
            from build_provenance where project_id=$1
            """,
            project.id,
        )
        recent = await conn.fetch(
            """
            select build_id, task_code, artifact_name, fabric_status, fabric_tx_id,
                   fabric_block, fabric_anchored, created_at
            from build_provenance where project_id=$1
            order by created_at desc limit 20
            """,
            project.id,
        )
        anchors = await conn.fetch(
            """
            select day, last_seq, hash, created_at from audit_anchors
            order by day desc limit 5
            """
        )

    ledger = fabric.ledger(limit=10) if fabric.enabled() else None
    stats = fabric.stats() if fabric.enabled() else None

    return {
        "totals": {
            "builds": row["builds"],
            "anchored": row["anchored"],
            "failed": row["failed"],
            "unanchored": row["unanchored"],
        },
        "fabric": {
            "enabled": fabric.enabled(),
            "channel": (ledger or {}).get("channel"),
            "chaincode": (ledger or {}).get("chaincode"),
            "block_height": (ledger or {}).get("block_height"),
            "total": (stats or {}).get("total"),
            "last_anchored": (stats or {}).get("last_anchored"),
            "error": (ledger or {}).get("error"),
            "recent": (ledger or {}).get("records") or [],
        },
        "builds": [
            {
                "build_id": item["build_id"],
                "task": item["task_code"],
                "artifact": item["artifact_name"],
                "status": item["fabric_status"],
                "tx_id": item["fabric_tx_id"],
                "block": item["fabric_block"],
                "anchored_at": item["fabric_anchored"].isoformat() if item["fabric_anchored"] else None,
                "created_at": item["created_at"].isoformat(),
            }
            for item in recent
        ],
        "audit_anchors": [
            {
                "day": item["day"].isoformat(),
                "seq": item["last_seq"],
                "hash": item["hash"],
                "created_at": item["created_at"].isoformat(),
            }
            for item in anchors
        ],
    }
