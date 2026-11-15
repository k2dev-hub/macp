"""Requirement document upload/download API."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from .auth import Principal, require_write_token
from .db import current_project
from .documents import MAX_BYTES, ingest

router = APIRouter(
    prefix="/v1/documents", tags=["documents"], dependencies=[Depends(require_write_token)]
)


def pool(request: Request):
    return request.app.state.pool


@router.post("", status_code=201)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(require_write_token),
):
    if not principal.allows("task:create"):
        raise HTTPException(403, "task:create scope required to upload requirement documents")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"file exceeds {MAX_BYTES} bytes")
    try:
        document = await asyncio.to_thread(
            ingest, current_project().code, file.filename or "document", data
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    async with pool(request).acquire() as conn:
        await conn.execute(
            """
            insert into task_documents (
              id, project_id, filename, media_type, size_bytes, sha256,
              storage_path, text_content, char_count, warnings, created_by
            ) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
            """,
            document["id"],
            current_project().id,
            document["filename"],
            document["media_type"],
            document["size_bytes"],
            document["sha256"],
            document["storage_path"],
            document["text_content"],
            document["char_count"],
            document["warnings"],
            uuid.UUID(principal.agent_id) if principal.agent_id else None,
        )
    return {
        "id": str(document["id"]),
        "filename": document["filename"],
        "media_type": document["media_type"],
        "size_bytes": document["size_bytes"],
        "sha256": document["sha256"],
        "char_count": document["char_count"],
        "warnings": document["warnings"],
    }


@router.get("")
async def list_documents(request: Request, task: str | None = None):
    async with pool(request).acquire() as conn:
        if task:
            rows = await conn.fetch(
                """
                select d.id, d.filename, d.media_type, d.size_bytes, d.sha256,
                       d.char_count, d.warnings, d.created_at, t.code as task
                from task_documents d left join tasks t on t.id=d.task_id
                where d.project_id=$1 and t.code=$2 order by d.created_at
                """,
                current_project().id,
                task,
            )
        else:
            rows = await conn.fetch(
                """
                select d.id, d.filename, d.media_type, d.size_bytes, d.sha256,
                       d.char_count, d.warnings, d.created_at, t.code as task
                from task_documents d left join tasks t on t.id=d.task_id
                where d.project_id=$1 order by d.created_at desc limit 200
                """,
                current_project().id,
            )
    return [dict(row) for row in rows]


@router.get("/{document_id}/text")
async def document_text(
    document_id: uuid.UUID,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20000, ge=1, le=50000),
):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            select filename, text_content, char_count from task_documents
            where id=$1 and project_id=$2
            """,
            document_id,
            current_project().id,
        )
    if not row:
        raise HTTPException(404, "document not found")
    text = row["text_content"]
    chunk = text[offset : offset + limit]
    return {
        "id": str(document_id),
        "filename": row["filename"],
        "offset": offset,
        "limit": limit,
        "char_count": row["char_count"],
        "text": chunk,
        "next_offset": offset + len(chunk) if offset + len(chunk) < len(text) else None,
    }


@router.get("/{document_id}")
async def download_document(document_id: uuid.UUID, request: Request):
    async with pool(request).acquire() as conn:
        row = await conn.fetchrow(
            """
            select filename, media_type, storage_path from task_documents
            where id=$1 and project_id=$2
            """,
            document_id,
            current_project().id,
        )
    if not row:
        raise HTTPException(404, "document not found")
    path = Path(row["storage_path"])
    if not path.exists():
        raise HTTPException(410, "stored document is missing")
    return FileResponse(path, media_type=row["media_type"], filename=row["filename"])
