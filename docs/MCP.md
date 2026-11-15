# MACP MCP 网关接入指南（Phase 1 只读 / Phase 2 写入）

MCP 网关把 MACP 的任务、仓库、需求文档以 MCP 工具和 resources 的形式暴露给支持
MCP 的 vibe coding 客户端（Claude Code / Cursor / Cline / Continue …）。
它是**适配层**：每个调用最终落到现有的 `POST /v1/tools/{name}/invoke`，
scope、写面、门禁、证据和事件链保持不变。

- 端点：`https://macp.example.com:9443/mcp`（只走 TLS 入口，`/mcp` 不在 80 端口）
- 传输：Streamable HTTP（JSON-RPC 2.0，单 JSON 响应）
- 认证：`Authorization: Bearer <mcp-token>`，token 由管理员签发，项目/Agent 绑定
- Phase 1 只读工具；Phase 2 增加 `macp_task_next`（任务绑定）、写工具、异步门禁 job、stdio bridge

## 1. 签发 token

```bash
ADMIN="$(cat /opt/macp/deploy/secrets/write_token)"
curl -sk -X POST https://macp.example.com:9443/v1/admin/tokens \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -H "X-MACP-Project: demo" \
  -d '{"agent":"worker-d","scopes":["mcp:connect","task:read","tool:invoke"],"ttl_minutes":1440}'
```

返回里的 `token` 只显示一次（`macp_…`），**不要**存进 git，也不要贴到聊天里。
建议落到 0600 文件：

```bash
install -m 600 /dev/null /opt/macp/deploy/secrets/mcp_readonly_token
# 把返回的 token 写进去
```

管理员 token 不能当 MCP token 用（没有 `mcp:connect`，会被 403 拒绝）。

## 2. 客户端配置

### 2.1 Claude Code / Cursor（远程 HTTP）

```json
{
  "mcpServers": {
    "macp": {
      "type": "http",
      "url": "https://macp.example.com:9443/mcp",
      "headers": {
        "Authorization": "Bearer <mcp-token>"
      }
    }
  }
}
```

自签证书需要客户端信任，或在客户端配置里允许 insecure TLS。

### 2.2 自检（官方 MCP Python SDK 2.x，已验证）

```bash
/opt/macp-mcp-client/bin/python deploy/verify/verify-mcp-client.py
```

等价的最小客户端（SDK 2.x 的 API：`streamable_http_client` + `httpx2`）：

```python
import asyncio, httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    token = open("/opt/macp/deploy/secrets/mcp_readonly_token").read().strip()
    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                verify=False, timeout=60)
    async with streamable_http_client("https://macp.example.com:9443/mcp",
                                      http_client=client) as (read, write, *_):
        async with ClientSession(read, write) as session:
            info = await session.initialize()          # info.server_info.name == "macp"
            tools = await session.list_tools()         # 9 个只读工具
            res = await session.call_tool("macp_task_get", {"task": "T-XXXX"})
            body = await session.read_resource("macp://task/T-XXXX")
    await client.aclose()

asyncio.run(main())
```

实测（2026-11-15）：`initialize handshake / protocol version / tools-list /
no-write-tools / tools-call / resources-read` 全部 PASS。
## 3. Phase 1 工具

| MCP 工具 | 作用 | 必需参数 |
|---|---|---|
| `macp_task_get` | 任务摘要/状态 | `task` |
| `macp_workorder_get` | Work Order JSON | `task` |
| `macp_repo_list` | 列目录 | `task`, `path?` |
| `macp_repo_read` | 读文件 | `task`, `path`, `start_line?`, `end_line?` |
| `macp_repo_search` | 全文搜索 | `task`, `query`, `max_results?` |
| `macp_document_list` | 需求文档列表 | `task` |
| `macp_document_read` | 读需求文档 | `task`, `document_id` |
| `macp_git_status` | 工作区状态 | `task` |
| `macp_git_diff` | 工作区 diff | `task`, `path?` |

所有工具带 `annotations.readOnlyHint=true`。写工具（`macp_shell_run`、
`macp_file_write`、`macp_gate_run` …）**不在 Phase 1 的 list 里**；按名字调用会得到
`isError=true` + `code=forbidden`。

## 4. Resources

| URI | 内容 |
|---|---|
| `macp://task/{code}` | 任务详情 |
| `macp://workorder/{code}` | Work Order |
| `macp://document/{id}?task={code}` | 需求文档正文 |

`resources/list` 会列出当前项目最近 20 个任务的 task 和 workorder 资源。

## 5. 错误

| 场景 | 表现 |
|---|---|
| 无 token / token 无效 | HTTP 401，JSON-RPC `-32001` |
| 缺 `mcp:connect` | HTTP 403，`-32003` |
| 未 initialize 或 session 过期 | `-32002 session_not_initialized` |
| 工具名不存在 | `-32602 unknown tool` |
| 写工具但 token 无 `mcp:write` | 工具结果 `isError=true`，`code=forbidden` |\n| 写工具但未绑定任务 | 工具结果 `isError=true`，`code=task_not_bound` |
| 缺 `task` 参数 | 工具结果 `isError=true`，`code=task_required` |
| 限流（60 req/min/session） | HTTP 429，`-32000 rate_limited` |

## 6. 运维

```bash
docker compose -f deploy/docker-compose.yml ps mcp
docker logs --tail 50 macp-mcp
curl -sk https://macp.example.com:9443/mcp -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}'
```

- session 存在内存里，单副本；网关重启后客户端需要重新 `initialize`
  （会收到 `-32002`）。
- 网关不落盘、不打印 token：token 只在 session 内存里用于转发调用。
- 改完 `deploy/nginx.conf` 后要 `docker compose up -d --force-recreate nginx`：
  它是文件级 bind mount，`git checkout` 换 inode 后 `nginx -s reload` 读到的还是旧文件。

---

## 7. Phase 2：任务绑定与写入

### 7.1 token

写能力需要额外 scope（只读 token 会被 `mcp:write` 拦住）：

```bash
curl -sk -X POST https://macp.example.com:9443/v1/admin/tokens \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -H "X-MACP-Project: demo" \
  -d '{"agent":"worker-d","ttl_minutes":60,"scopes":[
        "mcp:connect","mcp:write","task:claim","tool:invoke","repo:read",
        "workspace:read","workspace:write","gate:run","receipt:write",
        "task:heartbeat","task:release","agent:event","doc:read"]}'
```

### 7.2 一次完整任务

```text
macp_task_next                     认领 + 绑定 session（task token 留在网关内存）
macp_workorder_get                 读契约（绑定后不需要传 task）
macp_repo_read / document_read     读上下文
macp_file_write / file_patch       写文件
macp_gate_run  → job_id            异步门禁（构建可能远超客户端超时）
macp_job_wait {job_id, 30}         轮询，返回 exit / evidence_id
macp_git_commit / git_submit       提交并生成 candidate
macp_receipt_submit                回执（成功后自动解绑，可接下一个任务）
```

写工具**不接受 `task` 参数**：它们只作用于当前绑定的 attempt，防止越权指定任务。
`macp_receipt_submit` 或 `macp_task_block` 成功后绑定解除。

### 7.3 stdio bridge

只支持 stdio 的客户端用 `bridge/macp_mcp_bridge.py`：

```json
{
  "mcpServers": {
    "macp": {
      "command": "python3",
      "args": ["/opt/macp/bridge/macp_mcp_bridge.py"],
      "env": {
        "MACP_MCP_URL": "https://macp.example.com:9443/mcp",
        "MACP_MCP_TOKEN": "<mcp-token>"
      }
    }
  }
}
```

bridge 只做透明转发：从 stdin 读 JSON-RPC、带 `Mcp-Session-Id` 转发、把响应写回
stdout；token 只从环境变量读，不落盘、不打印。

### 7.4 端到端验证

```bash
python3 deploy/verify/verify-mcp-e2e.py
# claim → file_write → gate(job) → commit → submit → receipt → review → merged
# 并检查 tool.called 事件带 channel=mcp
```

