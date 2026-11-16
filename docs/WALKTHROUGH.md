# 代码导读（面向第一次接触这个仓库的人）

这份文档回答三个问题：**这套系统在做什么、代码怎么组织的、想改东西该从哪下手。**
不假设读者熟悉 FastAPI、PostgreSQL 或分布式系统，遇到概念会先解释再用。

阅读顺序建议：先看第 1 节建立概念，再看第 2 节的地图，然后按第 3 节的"跟一次请求"走一遍。
不要一上来读 `tool_registry.py`（60 KB），那是全仓库最厚的文件。

---

## 1. 三分钟理解这套系统

### 1.1 它要解决的问题

多个 AI Agent 一起改代码时，最难的不是让它们写出来，而是回答：

- 这段代码是谁写的、基于哪次提交？
- 它在什么环境里构建过、跑过哪些检查？
- 出了问题，能不能证明"当时确实是这个产物"？

普通 Git 只能回答"谁提交了什么"，回答不了"怎么构建的"。于是这套系统把
**任务、构建、证据、回执**串成一条链，任何一环被改都能被发现。

### 1.2 四个角色

| 角色 | 在代码里是什么 | 干什么 |
|---|---|---|
| **人** | `web/`（控制台）、`site/`（官网） | 建任务、看进度、审回执 |
| **Agent** | `agents/` 下的各个 `*_loop.py` | 领任务、调工具、交代码 |
| **平台** | `server/`（FastAPI 服务） | 鉴权、派单、跑门禁、发回执、记事件 |
| **执行体** | `server/runner.py`、`tool_registry.kvm_node_run` | 真正跑命令的地方（沙箱或虚拟机） |

### 1.3 四个核心概念（对应数据库表）

| 概念 | 表 | 一句话 |
|---|---|---|
| 任务 | `tasks` | 一个工作项，有状态（ready → in_progress → review → merged） |
| 工作项 | `work_orders` | 任务的规则：能改哪些文件、要过哪些门禁、在哪执行 |
| 证据 | `evidence` | 跑门禁留下的日志/产物，按 SHA-256 存 |
| 回执 | `receipts` | 交付凭证：候选分支 + 门禁结果 + 签名 |
| 事件 | `events` | 所有状态变化的追加日志，每条带前一条的哈希 |

表结构在 `deploy/init/`：`0001_init.sql` 是主体，`0002_provenance.sql` 与
`0003_fabric.sql` 是增量。看一遍就知道系统有什么数据。

---

## 2. 代码地图

```
macp/
├── server/            ← 平台后端（FastAPI），先读这里
│   ├── main.py            应用入口：建 app、挂路由、中间件、健康检查
│   ├── settings.py        配置加载（config/macp.toml），dev 时先看它
│   ├── db.py              数据库连接池 + emit_event（事件链的核心）
│   ├── auth.py            鉴权：管理员 token / 任务级 token / OIDC
│   ├── agent_api.py       任务生命周期：建任务、派单、认领、回执、评审
│   ├── tool_registry.py   工具网关：shell_run / gate_run / 写文件…（最厚）
│   ├── workorders.py      规则引擎：写面校验、风险分级、门禁要求
│   ├── observer.py        事件消费：推进投影、算指标、出告警
│   ├── merge_api.py       合并单车道：建合并请求、逐步执行、回滚
│   ├── provenance.py      构建溯源：记录指纹、签名、校验
│   ├── fabric.py          联盟链锚定（可选，通过本地 bridge）
│   └── runner.py          沙箱执行服务（bwrap），被 tool_registry 调用
├── agents/            ← Agent 进程，各是一个死循环
│   ├── worker_loop.py     领任务 → 用 LLM 决定调哪个工具 → 提交
│   ├── scheduler_loop.py  把就绪任务派给空闲 worker
│   ├── coordinator_loop.py 处理阻塞、重试、升级
│   ├── merge_loop.py      轮询合并队列
│   ├── llm_runtime.py     调 OpenAI 兼容接口，解析 tool_calls
│   └── mock_openai.py     假模型：没有 API key 也能演示全流程
├── web/               ← React 控制台（Ant Design）
├── site/              ← 对外官网（构建时预渲染成静态页）
├── deploy/            ← 部署、初始化 SQL、脚本、验证、最小版
│   ├── docker-compose.yml   完整栈（20 个容器）
│   ├── init/0001_init.sql   建表
│   ├── minimal/             11 容器最小版（演示/答辩用）
│   └── verify/              各种端到端验证脚本
├── signer/            ← 独立签名服务（唯一持有私钥的容器）
└── tests/             ← 少量冒烟测试
```

### 2.1 三个进程模型

系统不是一个大程序，而是三类进程互相通过 HTTP 和数据库协作：

```
浏览器 ──► nginx ──► api (FastAPI)  ──► PostgreSQL
                        ▲   │
                        │   └──► runner (沙箱) / KVM 节点 (虚拟机)
                        │
   worker / scheduler / coordinator / observer  ──┘
        （独立容器，各自是 while True 循环）
```

- **api** 是无状态的：所有状态在数据库里。
- **agent** 是循环进程：每轮查一次"有没有我要做的事"，没有就 sleep。
- **observer** 只读事件表，把事件投影成指标与告警。

理解这一点，很多"为什么改完没生效"就有答案了：改的是 api 还是 agent？容器重建了没有？

---

## 3. 跟一次请求走：从 HTTP 到数据库

以 `GET /v1/tasks` 为例（看板加载任务列表）。

**第 1 步：中间件**

`server/main.py` 里有两个 `@app.middleware("http")`：

- `project_context`：从请求头 `X-MACP-Project`（或默认值）确定"当前项目"，
  存进 `db.current_project()` 的上下文变量。后面所有 SQL 都用它过滤。
- `request_context`：给请求生成 `rid`，记访问日志。

> 新手常见疑问："为什么函数里没有 project 参数，SQL 却有 `project_id=$1`？"
> 答案就是这个上下文变量。它定义在 `db.py:41` 的 `current_project()`。

**第 2 步：鉴权**

`server/auth.py` 的 `authenticate()` 读 `Authorization: Bearer <token>`，判断它是
管理员 token（文件挂载）还是任务级 token（数据库里存哈希）。路由上写
`dependencies=[Depends(require_write_token)]` 就会先经过这里。

**第 3 步：路由函数**

`server/main.py:249` 的 `tasks()` 用 `pool(request).acquire()` 拿连接，写一条
`select ... from tasks where project_id=$1`，拼上依赖、负责人等信息返回 JSON。

> FastAPI 的套路：函数参数即请求参数，返回 dict 会自动变成 JSON。

**第 4 步：写操作才有事件**

读接口到此结束。凡是改状态的接口（建任务、认领、回执……）最后都会调用
`db.emit_event(...)`，在同一个事务里往 `events` 表追加一行。这一行包含
`prev_hash`，所以任何人都能从头到尾重新算一遍，验证没有被篡改。

---

## 4. 跟一次任务走：完整生命周期

这是全系统最重要的一条线，建议对着 `deploy/minimal/smoke.sh` 一起看，
那个脚本用 13 个断言把这条线走了一遍。

### 4.1 建任务

```
POST /v1/tasks  {code, title, work_order:{write_surface, acceptance, execution}}
        │
        ▼
agent_api.create_task()  →  写 tasks + work_orders，发 task.created 事件
```

`work_order` 是规则：这个任务允许改哪些路径、必须过哪些门禁、在哪种环境跑。
校验逻辑在 `server/workorders.py`（`path_allowed`、`required_gates`、`effective_level`）。

### 4.2 派单与认领

```
scheduler_loop  ──POST /v1/tasks/{code}/dispatch──►  建 assignment（谁来做）
worker_loop     ──GET  /v1/assignments/next──────►  取出待办
worker_loop     ──POST /v1/assignments/{id}/claim─►  认领，返回任务级 token
```

任务级 token 是关键设计：它绑定 `task` 和 `attempt`，权限只覆盖这一个任务。
`auth.py` 会拿 token 的哈希去 `agent_tokens` 表比对。

### 4.3 调工具

Agent 想执行命令，不能直接跑，必须走工具网关：

```
POST /v1/tools/{name}/invoke
        │
        ▼
tool_registry.invoke()  →  execute()  →  按 name 分派
        │
        ├─ workspace_write_file   写文件（先校验路径在工作项写面内）
        ├─ workspace_commit       git commit（用 agent 的密钥签名）
        ├─ shell_run              跑命令（必须声明 platform）
        └─ gate_run               跑门禁（编译/单测/构建…）
```

`execute()` 是一个很长的 `if name == ...` 函数（`tool_registry.py:798`）。
新手可以只看三个分支：`workspace_write_file`、`shell_run`、`gate_run`。

**执行画像**：`shell_run` 必须声明 `platform`（sandbox / linux / android / windows），
`run_command()` 按 `PLATFORM_PROFILES` 把它映射成执行方式：

| platform | 实际在哪跑 | 谁执行 |
|---|---|---|
| sandbox | 容器内的 bwrap | `runner.py` |
| linux / android / windows | 一次性虚拟机 | `kvm_node_run()` → 构建节点的 HTTP 接口 |

### 4.4 门禁与证据

`gate_run` 跑完会做三件事：

1. 把 stdout/stderr 存成日志文件，算 SHA-256，写 `evidence` 表；
2. 产物（build 类门禁）也存一份，记录哈希；
3. 生成**构建溯源记录**（`provenance.py`）：指纹 + 签名 + 一条 `provenance.recorded` 事件。

第 3 步让"这个产物从哪来"可以被任何人验证，也是 `GET /verify/{build_id}` 页面的数据来源。

### 4.5 回执与合并

```
workspace_submit  → 产出 refs/candidates/{task}/{attempt}
POST /v1/tasks/{code}/receipts  {candidate_ref, gates:[{gate, exit, evidence_id}]}
        │
        ▼
agent_api.submit_receipt()  校验门禁齐全 → 调 signer 签名 → 写 receipts → 任务进 review
        │
        ▼
merge_api.enqueue() → merge_worker 逐条执行 → 合并后复验 → merged
```

**签名走独立服务**：`signer/app.py` 是唯一挂载私钥的容器，api 只有公钥。
`server/signing.py` 负责调用，拿不到签名就报错，不会写占位值。

---

## 5. 如果想改点什么

| 想做的事 | 从哪开始 |
|---|---|
| 加一个工具（比如 `git_log`） | `tool_registry.py`：`TOOLS` 里加定义 + `execute()` 里加分支 |
| 改门禁命令 | `tool_registry.py` 的 `execute()` 中 `gate == "build"` 等分支 |
| 加一种执行平台 | `PLATFORM_PROFILES` + `known_profiles()` + `deploy/kvm-node/` |
| 改任务规则（风险分级/必过门禁） | `server/workorders.py` 顶部的常量表 |
| 加一张表 | 新增 `deploy/init/0004_*.sql`，本地重建数据库即生效（已运行的库需手动应用） |
| 改控制台界面 | `web/src/HumanApp.tsx`（主界面）、`web/src/api.ts`（接口封装） |
| 改官网 | `site/src/App.tsx`；改完 `npm run build:all` 重新预渲染 |
| 改配置项 | `config/macp.example.toml` + `server/settings.py` 的 `DEFAULTS` |

改完记得跑 `python3 -m server.settings doctor` 和 `./deploy/release-check.sh`。

---

## 6. 本地跑起来并观察

```bash
./deploy/bootstrap.sh                       # 生成配置、密钥、TLS、自检
docker compose -f deploy/docker-compose.yml up -d --build
```

- 控制台：`https://localhost:9443/`
- 管理员 token：`deploy/secrets/write_token`
- 健康检查：`curl -k https://localhost:9443/v1/health`

机器较弱时用最小版（11 个容器、约 300 MB 内存）：

```bash
./deploy/minimal/up.sh          # 起服务
./deploy/minimal/smoke.sh       # 跑一遍完整审计闭环，逐项断言
```

### 调试常用的四个地方

| 想确认 | 命令 |
|---|---|
| 服务活着吗 | `curl -k https://localhost:9443/v1/health` |
| 刚才那个请求发生了什么 | `docker logs --tail 50 macp-api` |
| 数据库里到底存了什么 | `docker exec -it macp-db psql -U macp -d macp -c "select code,state from tasks"` |
| 事件链有没有断 | `curl -k -H "Authorization: Bearer $TOKEN" https://localhost:9443/v1/audit/chain` |

> 注意：`deploy/docker-compose.yml` 里 `nginx.conf` 是单文件挂载，替换后必须
> `docker compose up -d --force-recreate nginx`，否则容器还拿着旧文件。

---

## 7. 新手最容易踩的五个坑

1. **执行画像名不是平台名。** 平台是 `sandbox/linux/android/windows`，
   画像是 `local/kvm-debian/android-debian/windows-ltsc`。写错会报
   `unknown execution profile`。
2. **先提交再跑门禁。** 门禁会把 `.evidence/` 写进工作区，如果先跑门禁再
   `workspace_commit`，提交会因证据文件越出写面而回滚成空提交，
   随后 `workspace_submit` 报 `no commit to submit`。
3. **容器名和主机名不是一回事。** api 连数据库用 `db`，宿主连数据库用
   `127.0.0.1:15432`（隧道/端口映射）。
4. **改代码后要重建镜像。** `docker compose restart` 不会重新构建，
   改的是镜像里的文件，必须 `up -d --build`。
5. **别把"演示"当"完成"。** 仓库的判断标准是 `./deploy/release-check.sh`
   全绿，它跑真实构建、真实 E2E，不是看接口返回 200。

---

## 8. 推荐阅读顺序（约 4 小时）

| 顺序 | 文件 | 目的 | 预计 |
|---|---|---|---|
| 1 | `README.md` | 知道它是什么 | 10 分钟 |
| 2 | `deploy/init/0001_init.sql` | 认识数据模型 | 20 分钟 |
| 3 | `server/main.py` | 看入口、路由、中间件 | 30 分钟 |
| 4 | `server/db.py` 的 `emit_event` | 理解事件链 | 20 分钟 |
| 5 | `server/agent_api.py` 的 `create_task` / `submit_receipt` | 任务生命周期 | 40 分钟 |
| 6 | `server/tool_registry.py` 的 `execute`（只看三个分支） | 工具与门禁 | 60 分钟 |
| 7 | `agents/worker_loop.py` | Agent 怎么用工具 | 30 分钟 |
| 8 | `server/provenance.py` | 溯源与验签 | 30 分钟 |
| 9 | `deploy/minimal/smoke.sh` | 把上面串起来 | 20 分钟 |

读完这九步，就能独立定位"某个功能在哪实现、改它会影响什么"。

---

## 9. 术语对照表

| 中文件里出现的说法 | 代码里叫什么 |
|---|---|
| 工作项 / 工单 | `work_order`（`workorders.py`） |
| 写面 | `write_surface`（允许修改的路径模式） |
| 执行画像 | `execution_profile`（`local`、`kvm-debian`…） |
| 平台 | `platform`（`sandbox`、`linux`…） |
| 门禁 | `gate`（`compile`、`unit`、`build`…） |
| 证据 | `evidence`（日志/产物的哈希） |
| 回执 | `receipt`（候选分支 + 门禁结果 + 签名） |
| 认领 | `claim`（拿到任务级 token） |
| 事件链 | `events` 表的 `prev_hash` 链 |
| 锚点 | `audit_anchors`（每天钉住链头） |
| 构建溯源 | `build_provenance`（产物指纹记录） |
| 联盟链锚定 | `[fabric]` 配置 + `fabric.py` |

---

有疑问时，最快的定位方式是：**先在数据库里找到那条记录，再看是谁写进去的**
（`grep -rn "insert into <表名>" server/`），通常一两步就能找到入口函数。
