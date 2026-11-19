"""Audit board: a live view of what the platform is doing, for demos and reviews.

Two endpoints:

  GET /v1/audit/summary   counters, in-flight tasks and chain state (token)
  GET /audit              the board itself (public HTML, reads the stream)

The board listens to `/v1/events/stream` (already unauthenticated, same origin),
so a room can watch a build happen without touching a terminal.
"""

from __future__ import annotations

import html
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .auth import require_write_token
from .db import current_project

router = APIRouter(tags=["audit"])

ACTIVE_STATES = ("ready", "assigned", "in_progress", "review", "blocked", "blocked_review")

_SUMMARY_SQL = """
select
  (select count(*) from events where project_id=$1) as events,
  (select max(seq) from events where project_id=$1) as last_seq,
  (select hash from events where project_id=$1 order by seq desc limit 1) as head_hash,
  (select count(*) from receipts where project_id=$1) as receipts,
  (select count(*) from build_provenance where project_id=$1) as builds,
  (select count(*) from build_provenance where project_id=$1 and fabric_status='anchored') as anchored,
  (select count(*) from events where project_id=$1 and type='gate.passed') as gates_passed,
  (select count(*) from events where project_id=$1 and type='gate.failed') as gates_failed
"""

# tasks 没有 assignee 列：负责人来自当前有效的 assignment
_TASKS_SQL = """
select t.code, t.title, t.state, ag.code as agent, t.updated_at
from tasks t
left join lateral (
  select a.agent_id
  from assignments a
  where a.task_id = t.id and a.state in ('offered','claimed','active')
  order by a.created_at desc
  limit 1
) a on true
left join agents ag on ag.id = a.agent_id
where t.project_id=$1 and t.state = any($2::text[])
order by t.updated_at desc
limit 8
"""


@router.get("/v1/audit/summary", dependencies=[Depends(require_write_token)])
async def audit_summary(request: Request):
    """Everything the board needs in one round-trip."""

    conn = request.app.state.pool
    project = current_project()

    from . import fabric

    async with conn.acquire() as connection:
        row = await connection.fetchrow(_SUMMARY_SQL, project.id)
        tasks = await connection.fetch(_TASKS_SQL, project.id, list(ACTIVE_STATES))

    ledger = fabric.ledger(limit=8) if fabric.enabled() else None
    stats = fabric.stats() if fabric.enabled() else None
    records = (ledger or {}).get("records") or []

    return {
        "counters": {
            "events": row["events"] or 0,
            "receipts": row["receipts"] or 0,
            "builds": row["builds"] or 0,
            "anchored": row["anchored"] or 0,
            "gates_passed": row["gates_passed"] or 0,
            "gates_failed": row["gates_failed"] or 0,
        },
        "chain": {
            "enabled": fabric.enabled(),
            "channel": (ledger or {}).get("channel"),
            "chaincode": (ledger or {}).get("chaincode"),
            "height": (ledger or {}).get("block_height"),
            "total": (stats or {}).get("total"),
            "last_anchored": (stats or {}).get("last_anchored"),
            "error": (ledger or {}).get("error"),
            "recent": [
                {
                    "build_id": item.get("build_id"),
                    "fingerprint": (item.get("fingerprint") or "")[:16],
                    "anchored_at": item.get("anchored_at"),
                    "tx_id": (item.get("tx_id") or "")[:16],
                }
                for item in records
            ],
        },
        "audit": {
            "last_seq": row["last_seq"] or 0,
            "head_hash": row["head_hash"],
            "now": datetime.now(timezone.utc).isoformat(),
        },
        "tasks": [
            {
                "code": task["code"],
                "title": task["title"],
                "state": task["state"],
                "agent": task["agent"],
                "updated_at": task["updated_at"].isoformat(),
            }
            for task in tasks
        ],
    }


_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>审计看板 · MACP</title>
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin: 0; background: #0d1117; color: #d7dde5;
        font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
 header { display: flex; align-items: center; gap: 16px; padding: 14px 22px;
          border-bottom: 1px solid #1f2733; background: #11161d; }
 header h1 { margin: 0; font-size: 17px; font-weight: 600; letter-spacing: .3px; }
 .dot { width: 8px; height: 8px; border-radius: 50%; background: #2ea043; display: inline-block;
        box-shadow: 0 0 0 4px rgba(46,160,67,.15); animation: pulse 2s infinite; }
 .dot.off { background: #d1242f; box-shadow: 0 0 0 4px rgba(209,36,47,.15); animation: none; }
 @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .45 } }
 .muted { color: #7d8794; font-size: 12px; }
 .grow { flex: 1; }
 .stats { display: flex; gap: 26px; padding: 14px 22px; border-bottom: 1px solid #1f2733; }
 .stat b { font-size: 22px; font-weight: 600; display: block; line-height: 1.25; }
 .stat span { font-size: 11px; color: #7d8794; }
 main { display: grid; grid-template-columns: 1fr 380px; gap: 1px; background: #1f2733;
        height: calc(100vh - 118px); }
 section { background: #0d1117; overflow: hidden; display: flex; flex-direction: column; }
 h2 { margin: 0; padding: 12px 18px; font-size: 13px; font-weight: 600; color: #9aa5b1;
      border-bottom: 1px solid #1f2733; display: flex; align-items: center; gap: 10px; }
 #feed { flex: 1; overflow-y: auto; padding: 6px 0; font-size: 13px; }
 .row { display: grid; grid-template-columns: 62px 96px 1fr 110px; gap: 10px;
        padding: 7px 18px; border-bottom: 1px solid #151b24; align-items: baseline; }
 .row.new { animation: flash 1.6s ease-out; }
 @keyframes flash { from { background: #17251a } to { background: transparent } }
 .t { color: #6e7a88; font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; }
 .badge { font-size: 11px; padding: 1px 7px; border-radius: 10px; white-space: nowrap;
          text-align: center; }
 .b-task { background: #1f6feb22; color: #6ea8fe; }
 .b-gate { background: #2ea04322; color: #56d364; }
 .b-receipt { background: #d2992222; color: #e3b341; }
 .b-chain { background: #a371f722; color: #bc8cff; }
 .b-merge { background: #f8514922; color: #ff7b72; }
 .b-other { background: #30363d; color: #9aa5b1; }
 .sum { color: #c9d1d9; }
 .sum small { color: #7d8794; }
 .task { color: #6e7a88; font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; }
 #tasks, #chain { padding: 8px 0; overflow-y: auto; }
 .trow { display: flex; align-items: center; gap: 8px; padding: 8px 18px;
         border-bottom: 1px solid #151b24; font-size: 12.5px; }
 .chip { font-size: 10.5px; padding: 1px 7px; border-radius: 9px; background: #30363d;
         color: #9aa5b1; }
 .s-ready { background: #1f6feb33; color: #6ea8fe; }
 .s-in_progress { background: #2ea04333; color: #56d364; }
 .s-review { background: #d2992233; color: #e3b341; }
 .s-blocked { background: #f8514933; color: #ff7b72; }
 .chainbox { padding: 12px 18px; border-bottom: 1px solid #151b24; }
 .kv { display: flex; justify-content: space-between; font-size: 12px; padding: 3px 0; }
 .kv span:first-child { color: #7d8794; }
 code { font-family: ui-monospace, Consolas, monospace; }
 .anchor { padding: 6px 18px; font-size: 11.5px; border-bottom: 1px solid #151b24;
           display: flex; justify-content: space-between; gap: 8px; }
 .anchor code { color: #bc8cff; }
</style></head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>审计看板</h1>
  <span class="muted" id="conn">连接中…</span>
  <span class="grow"></span>
  <span class="muted" id="project"></span>
  <span class="muted" id="clock"></span>
</header>
<div class="stats">
  <div class="stat"><b id="c-events">0</b><span>审计事件</span></div>
  <div class="stat"><b id="c-gates">0</b><span>门禁通过</span></div>
  <div class="stat"><b id="c-receipts">0</b><span>签名回执</span></div>
  <div class="stat"><b id="c-builds">0</b><span>构建记录</span></div>
  <div class="stat"><b id="c-anchored">0</b><span>已上链</span></div>
  <div class="stat"><b id="c-height">-</b><span>区块高度</span></div>
</div>
<main>
  <section>
    <h2>实时事件流 <span class="muted" id="head"></span></h2>
    <div id="feed"></div>
  </section>
  <section>
    <h2>进行中任务</h2>
    <div id="tasks"></div>
    <h2>联盟链</h2>
    <div id="chain"></div>
  </section>
</main>
<script>
const KEY = "macp_admin_token";
const fromUrl = new URLSearchParams(location.search).get("token");
if (fromUrl) window.localStorage.setItem(KEY, fromUrl);
const token = window.localStorage.getItem(KEY) || "";
const project = new URLSearchParams(location.search).get("project")
             || window.localStorage.getItem("macp_project") || "demo";
document.getElementById("project").textContent = "项目 " + project;

const KIND = [
  [/^task\\./, "task", "b-task"], [/^gate\\./, "gate", "b-gate"],
  [/^receipt\\./, "receipt", "b-receipt"], [/^provenance\\./, "chain", "b-chain"],
  [/^(anchor|fabric)\\./, "chain", "b-chain"], [/^merge\\./, "merge", "b-merge"],
];
function kindOf(type) {
  for (const [re, name, cls] of KIND) if (re.test(type)) return [name, cls];
  return ["other", "b-other"];
}
function shortHash(h) { return h ? h.replace("sha256:", "").slice(0, 10) : ""; }
function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}
function summarize(ev) {
  const p = ev.payload || {};
  const t = ev.type;
  const map = {
    "task.created": () => `创建任务 ${p.code || ev.task || ""}`,
    "task.assigned": () => `派单给 ${p.agent || ""}`,
    "task.started": () => `开始执行`,
    "attempt.created": () => `第 ${p.attempt || 1} 次尝试`,
    "assignment.claimed": () => `${ev.actor_id ? "" : ""}认领任务`,
    "gate.passed": () => `门禁 ${p.gate || ""} 通过`,
    "gate.failed": () => `门禁 ${p.gate || ""} 失败`,
    "artifact.produced": () => `产物 ${p.name || ""} 已生成`,
    "provenance.recorded": () => `构建记录 ${p.build_id || ""} 已签名`,
    "receipt.submitted": () => `回执 ${p.receipt || ""} 已提交`,
    "review.approved": () => `验收通过`,
    "merge.queued": () => `进入合并队列`,
    "merge.merged": () => `合并完成`,
    "task.blocked": () => `任务阻塞`,
    "task.reworked": () => `打回返工`,
  };
  const fn = map[t];
  return fn ? fn() : t;
}

const feed = document.getElementById("feed");
const seen = new Set();
let paused = false;

function addRow(ev) {
  if (paused || seen.has(ev.seq)) return;
  seen.add(ev.seq);
  const [name, cls] = kindOf(ev.type);
  const row = document.createElement("div");
  row.className = "row new";
  row.innerHTML =
    `<span class="t">${fmtTime(ev.occurred_at)}</span>` +
    `<span class="badge ${cls}">${name}</span>` +
    `<span class="sum">${summarize(ev)} <small>${ev.type}</small></span>` +
    `<span class="task">${ev.task || ""}</span>`;
  feed.prepend(row);
  while (feed.children.length > 300) feed.lastChild.remove();
}

function connect() {
  const es = new EventSource(`/v1/events/stream?since=0&project=${encodeURIComponent(project)}`);
  const dot = document.getElementById("dot");
  const conn = document.getElementById("conn");
  es.onopen = () => { dot.classList.remove("off"); conn.textContent = "实时连接"; };
  es.onerror = () => { dot.classList.add("off"); conn.textContent = "重连中…"; };
  es.onmessage = (msg) => { try { addRow(JSON.parse(msg.data)); } catch (_) {} };
  for (const t of ["task.created","task.assigned","gate.passed","gate.failed",
                   "provenance.recorded","receipt.submitted","merge.merged"]) {
    es.addEventListener(t, (msg) => { try { addRow(JSON.parse(msg.data)); } catch (_) {} });
  }
}

async function refreshSummary() {
  if (!token) {
    document.getElementById("conn").textContent = "只读：未配置 token";
    return;
  }
  try {
    const response = await fetch(`/v1/audit/summary?project=${encodeURIComponent(project)}`,
                                 { headers: { Authorization: "Bearer " + token } });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    const c = data.counters;
    document.getElementById("c-events").textContent = c.events;
    document.getElementById("c-gates").textContent = c.gates_passed;
    document.getElementById("c-receipts").textContent = c.receipts;
    document.getElementById("c-builds").textContent = c.builds;
    document.getElementById("c-anchored").textContent = c.anchored;
    document.getElementById("c-height").textContent = (data.chain || {}).height ?? "-";
    document.getElementById("head").textContent =
      data.audit.head_hash ? "链头 " + shortHash(data.audit.head_hash) : "";

    const tasks = document.getElementById("tasks");
    tasks.innerHTML = (data.tasks || []).map((t) =>
      `<div class="trow"><span class="chip s-${t.state}">${t.state}</span>` +
      `<span>${t.code}</span><span class="muted">${t.assignee || ""}</span></div>`
    ).join("") || '<div class="trow muted">暂无进行中的任务</div>';

    const chain = data.chain || {};
    const box = document.getElementById("chain");
    box.innerHTML =
      `<div class="chainbox">` +
      `<div class="kv"><span>通道</span><span>${chain.channel || "-"}</span></div>` +
      `<div class="kv"><span>链码</span><span>${chain.chaincode || "-"}</span></div>` +
      `<div class="kv"><span>区块高度</span><span>${chain.height ?? "-"}</span></div>` +
      `<div class="kv"><span>锚定总数</span><span>${chain.total ?? "-"}</span></div>` +
      `<div class="kv"><span>最近锚定</span><span>${(chain.last_anchored || "").replace("T", " ").slice(0, 19)}</span></div>` +
      `</div>` +
      ((chain.recent || []).map((a) =>
        `<div class="anchor"><code>${(a.build_id || "").slice(-8)}</code>` +
        `<span class="muted">${(a.fingerprint || "").slice(0, 12)}…</span>` +
        `<span class="muted">${(a.tx_id || "").slice(0, 8)}…</span></div>`).join(""));
  } catch (error) {
    document.getElementById("conn").textContent = "统计不可用：" + error.message;
  }
}

setInterval(() => {
  document.getElementById("clock").textContent =
    new Date().toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);

connect();
refreshSummary();
setInterval(refreshSummary, 3000);
</script>
</body></html>"""


@router.get("/audit", response_class=HTMLResponse)
async def audit_page():
    """The board. Reads the public event stream; token only affects the counters."""

    return HTMLResponse(_PAGE)
