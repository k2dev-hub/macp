"""Build provenance API: look up a build, verify it, and show it to a stranger.

`/v1/provenance/...` requires the project token like the rest of the API.
`/verify/<build_id>` is deliberately public and read-only: that is the page a
QR code on a shipped artifact points at.  It shows hashes and verdicts, never
source, paths or tokens.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from .auth import require_write_token
from .db import current_project
from .artifact_stamp import build_zip
from .provenance import verify_record

router = APIRouter(tags=["provenance"])

WORKSPACE_ROOT = Path(os.getenv("MACP_WORKSPACE_ROOT", "/app/workspace"))

_SELECT = """
  select b.*, p.code as project_code
  from build_provenance b
  join projects p on p.id = b.project_id
"""


def pool(request: Request):
    return request.app.state.pool


def _row_to_dict(row) -> dict:
    data = dict(row)
    data["created_at_iso"] = data["created_at"].isoformat(timespec="seconds")
    for key, default in (("build_command", []), ("build_env", {}), ("dependencies", [])):
        value = data.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = default
        data[key] = value if value is not None else default
    return data


def _artifact_root(task_code: str, attempt: int) -> Path | None:
    """Where the gate kept the artifact inside the task workspace."""

    for candidate in (
        WORKSPACE_ROOT / "tasks" / task_code / str(attempt),
        WORKSPACE_ROOT / "tasks" / task_code / f"attempt-{attempt}",
        WORKSPACE_ROOT / "tasks" / task_code,
    ):
        if candidate.exists():
            return candidate
    return None


def _artifact_root_for(row: dict) -> Path | None:
    root = _artifact_root(row["task_code"], int(row.get("attempt") or 1))
    if root is not None and (root / (row.get("artifact_path") or "")).exists():
        return root
    return None


async def _load(conn, build_id: str) -> dict:
    row = await conn.fetchrow(
        _SELECT + " where b.build_id = $1 order by b.created_at desc limit 1", build_id
    )
    if row is None:
        raise HTTPException(404, f"unknown build id {build_id}")
    return _row_to_dict(row)


@router.get("/v1/provenance", dependencies=[Depends(require_write_token)])
async def list_provenance(
    request: Request,
    artifact_sha256: str | None = Query(default=None),
    task: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
):
    project = current_project()
    conn = pool(request)
    clauses = ["b.project_id = $1"]
    params: list = [project.id]
    if artifact_sha256:
        clauses.append(f"b.artifact_sha256 = ${len(params) + 1}")
        params.append(artifact_sha256)
    if task:
        clauses.append(f"b.task_code = ${len(params) + 1}")
        params.append(task)
    rows = await conn.fetch(
        _SELECT + f" where {' and '.join(clauses)} order by b.created_at desc limit {limit}",
        *params,
    )
    return [_row_to_dict(row) for row in rows]


@router.get("/v1/provenance/{build_id}", dependencies=[Depends(require_write_token)])
async def get_provenance(request: Request, build_id: str, verify: bool = Query(default=True)):
    conn = pool(request)
    row = await _load(conn, build_id)
    result: dict = {"record": row}
    if verify:
        result["verification"] = verify_record(row, artifact_root=_artifact_root_for(row))
    return result


@router.get("/v1/provenance/{build_id}/trace", dependencies=[Depends(require_write_token)])
async def trace_provenance(request: Request, build_id: str):
    """产物 → 构建 → 源码/依赖 → 链上事件，逐级回溯。"""

    conn = pool(request)
    row = await _load(conn, build_id)
    events = await conn.fetch(
        """
        select seq, type, occurred_at
        from events
        where project_id = $3
          and type in ('provenance.recorded','artifact.produced','receipt.signed')
          and (payload->>'build_id' = $1 or payload->>'task' = $2)
        order by seq desc
        limit 50
        """,
        build_id,
        row["task_code"],
        row["project_id"],
    )
    return {
        "artifact": {
            "name": row["artifact_name"],
            "sha256": row["artifact_sha256"],
            "size": row["artifact_size"],
        },
        "build": {
            "build_id": row["build_id"],
            "gate": row["gate"],
            "platform": row["platform"],
            "command": row["build_command"],
            "environment": row["build_env"],
            "builder": row["builder"],
            "created_at": row["created_at_iso"],
        },
        "source": {"commit": row["source_commit"], "ref": row["source_ref"]},
        "dependencies": {"count": row["dependency_count"], "items": row["dependencies"]},
        "chain": [
            {
                "seq": event["seq"],
                "event": event["type"],
                "at": event["occurred_at"].isoformat(timespec="seconds"),
            }
            for event in events
        ],
    }


@router.get("/v1/provenance/{build_id}/chain-anchor", dependencies=[Depends(require_write_token)])
async def chain_anchor(request: Request, build_id: str):
    """Read the record back from the consortium chain, not from our database."""

    from . import fabric

    if not fabric.enabled():
        return {"found": False, "detail": "fabric is not configured"}
    row = await _load(pool(request), build_id)
    answer = fabric.query(build_id) or {"found": False}
    record = answer.get("record") or {}
    if record:
        answer["fingerprint_matches"] = record.get("fingerprint") == row.get("fingerprint")
    return answer


# --------------------------------------------------------------------------- #
# public verification page
# --------------------------------------------------------------------------- #

_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>构建溯源 · {build_id}</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        margin: 0; background: #f5f6f8; color: #1c1f23; }}
 .card {{ max-width: 720px; margin: 32px auto; background: #fff; border-radius: 14px;
          box-shadow: 0 2px 16px rgba(0,0,0,.08); overflow: hidden; }}
 .head {{ padding: 22px 26px; background: {head_bg}; color: #fff; }}
 .head h1 {{ margin: 0 0 6px; font-size: 20px; font-weight: 600; }}
 .head p {{ margin: 0; opacity: .9; font-size: 13px; }}
 .body {{ padding: 8px 26px 24px; }}
 table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
 td {{ padding: 10px 0; border-bottom: 1px solid #eceef1; vertical-align: top; }}
 td.k {{ width: 30%; color: #646b73; }}
 code {{ font-family: ui-monospace, Consolas, monospace; font-size: 12.5px;
         word-break: break-all; }}
 .ok {{ color: #0a7d33; font-weight: 600; }}
 .bad {{ color: #b3261e; font-weight: 600; }}
 .checks {{ margin: 14px 0 0; padding: 0; list-style: none; font-size: 13px; }}
 .checks li {{ padding: 4px 0; }}
 .foot {{ padding: 14px 26px 22px; font-size: 12px; color: #7a828a; }}
</style></head>
<body><div class="card">
 <div class="head"><h1>构建溯源验证</h1><p>{verdict_line}</p></div>
 <div class="body">
  <table>
   <tr><td class="k">软件名称</td><td>{software}</td></tr>
   <tr><td class="k">构建编号</td><td><code>{build_id}</code></td></tr>
   <tr><td class="k">版本（源码）</td><td><code>{commit}</code></td></tr>
   <tr><td class="k">构建时间</td><td>{built_at}</td></tr>
   <tr><td class="k">构建人</td><td>{builder}</td></tr>
   <tr><td class="k">构建方式</td><td>{gate} · {platform}</td></tr>
   <tr><td class="k">产物指纹</td><td><code>{sha256}</code></td></tr>
   <tr><td class="k">依赖清单</td><td>{deps} 项</td></tr>
   <tr><td class="k">上链时间</td><td>{chained_at}</td></tr>
   <tr><td class="k">联盟链交易</td><td><code>{fabric_tx}</code></td></tr>
   <tr><td class="k">验证结果</td><td class="{verdict_class}">{verdict}</td></tr>
  </table>
  <ul class="checks">{check_items}</ul>
 </div>
 <div class="foot">记录以哈希链存证；任何字段或产物被改动，验证都会失败。</div>
</div></body></html>"""


@router.get("/verify/{build_id}", response_class=HTMLResponse)
async def public_verify(request: Request, build_id: str):
    conn = pool(request)
    row = await _load(conn, build_id)
    verification = verify_record(row, artifact_root=_artifact_root_for(row))

    chained = await conn.fetchval(
        "select occurred_at from events where project_id = $2 and type = 'provenance.recorded' "
        "and payload->>'build_id' = $1 order by seq desc limit 1",
        build_id,
        row["project_id"],
    )
    deps = row["dependencies"] or []
    check_items = "".join(
        f"<li><span class=\"{'ok' if item['ok'] else 'bad'}\">"
        f"{'✓' if item['ok'] else '✗'}</span> {html.escape(item['detail'])}</li>"
        for item in verification["checks"]
    )
    page = _PAGE.format(
        build_id=html.escape(build_id),
        head_bg="#0a7d33" if verification["verified"] else "#b3261e",
        verdict_line="记录完整，产物未被篡改" if verification["verified"] else "验证未通过，请勿使用该产物",
        verdict="✓ 未被篡改" if verification["verified"] else "✗ 验证失败",
        verdict_class="ok" if verification["verified"] else "bad",
        software=html.escape(row["artifact_name"] or ""),
        commit=html.escape((row["source_commit"] or "")[:12]),
        built_at=html.escape(row["created_at_iso"]),
        builder=html.escape(row["builder"] or ""),
        gate=html.escape(row["gate"] or ""),
        platform=html.escape(row["platform"] or ""),
        sha256=html.escape(row["artifact_sha256"] or ""),
        deps=len(deps) if deps else row["dependency_count"],
        chained_at=html.escape(chained.isoformat(timespec="seconds") if chained else "待入链"),
        fabric_tx=html.escape(row.get("fabric_tx_id") or "未锚定"),
        check_items=check_items,
    )
    return HTMLResponse(page)


@router.get("/v1/provenance/{build_id}/stamp.zip", dependencies=[Depends(require_write_token)])
async def stamp_package(request: Request, build_id: str):
    """The files that travel with the artifact: record, QR, how to verify."""

    conn = pool(request)
    row = await _load(conn, build_id)
    base_url = os.getenv("MACP_PUBLIC_URL", "").strip() or str(request.base_url).rstrip("/")
    payload = build_zip(row, base_url)
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{build_id}.stamp.zip"'},
    )


@router.get("/verify/{build_id}/qr.svg")
async def verify_qr(request: Request, build_id: str):
    """QR code for the verification URL, so an artifact can carry a sticker."""

    target = str(request.base_url).rstrip("/") + f"/verify/{build_id}"
    try:
        import segno
    except Exception:  # pragma: no cover - fallback when the dependency is absent
        raise HTTPException(501, "QR rendering requires the segno package")
    svg = segno.make(target, error="m").svg_inline(scale=4, border=2)
    return Response(svg, media_type="image/svg+xml")
