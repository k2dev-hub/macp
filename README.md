# MACP — 多 Agent 协作平面

[English](README.en.md) · **中文**

MACP 把多 Agent 编码组织成一条可审计的流水线：任务图是唯一真相源，
每个产物进 Git，每次交付附带签名回执，每条事件写入哈希链。Agent 在沙箱或一次性
虚拟机中执行，门禁通过方可合并。

## 它解决什么

多 Agent 系统最容易失控的地方不是模型能力，而是"谁在什么时候、基于什么证据、
改动了什么"。下列机制将该过程固化为可核查的记录：

- **任务图是真相源**：工作项（Work Order）声明写面、验收门禁和执行画像，代码仓只是产物；
- **门禁绑定执行环境**：在哪种环境里构建，就在哪种环境里复验（Linux / Android / Windows 各自的门禁）；
- **交付可验证**：候选 ref → 门禁结果 → Ed25519 签名回执 → 合并；验证者用公钥独立复算；
- **历史可追溯**：追加式事件链 + 每日锚点 + 异地镜像，改一条历史就对不上哈希；
- **联盟链锚定（可选）**：`[fabric]` 启用后，每条构建记录的**指纹**另提交到 Fabric 通道，
  API 只认一个本地 bridge 地址，链码保证同编号同指纹幂等、异指纹拒绝；
- **标签包**：每次构建同时生成 `provenance.json`、`verify-qr.svg`、`verify-qr.png` 和说明书，
  与产物一起归档，可随产物分发或贴标；

- **合并单车道**：同一时刻只有一个合并者，合并后再复验一次，失败可回滚。

## 快速开始

```bash
git clone https://github.com/k2dev-hub/macp.git && cd macp
./deploy/bootstrap.sh                          # 生成配置、密钥、TLS、自检
docker compose -f deploy/docker-compose.yml up -d --build
```

浏览器打开 `https://localhost:9443/`。管理员 token 在 `deploy/secrets/write_token`
（首次启动生成，0600，不入库、不打印）。

不需要任何模型 API key：默认接的是内置的 `mock-openai`，能把
"派单 → 写码 → 门禁 → 回执 → 合并"整条链路跑通。要接真模型就改
`config/macp.toml` 里的大模型配置，或直接把 `deploy/secrets/deepseek_api_key` 填上。

## 架构

```
       浏览器 / CLI / MCP 客户端
                 │
            nginx (TLS)                      只读看板 8088 / 完整入口 9443
                 │
   ┌─────────────┴──────────────┐
   │  API (FastAPI + SSE)       │  MCP 网关 /mcp（21 个工具）
   └─────────────┬──────────────┘
                 │
   调度器 · 协调者 · 合并者 · 观察者 · 签名者 · 执行器
                 │
   ┌─────────┬───┴────┬──────────┬────────────┐
 沙箱       Linux VM  Android VM Windows VM   PostgreSQL
 (bwrap)    (KVM)     (KVM)      (静态宿主)    事件 / 任务 / 回执
```

一次交付的路径：

1. 调度器把就绪任务派给空闲 worker，worker 认领并拿到**任务绑定的临时 token**；
2. worker 调工具（`shell_run`、`gate_run`、`workspace_write_file`…），工具按任务的
   Work Order 校验写面与平台，越界直接 403；
3. 门禁在同一执行画像里跑真实构建，产物 SHA-256 入链；
4. 提交候选 ref，签名者签发回执，观察者记录事件；
5. 合并者按单车道合入，合并后复验，失败回滚。

## 执行画像

| 平台 | 载体 | 用途 |
|---|---|---|
| `sandbox` | bwrap 沙箱 | 快速试跑、解释型脚本 |
| `linux` | 一次性 Debian VM | `make` / `cargo` / 真实 ELF 产物 |
| `android` | 一次性 Android VM | `gradle assembleDebug` → APK（含签名与校验） |
| `windows` | Windows LTSC 构建机 | `cmd` / MSBuild → PE 产物 |

`shell_run` 必须声明平台，系统路由到对应执行体；任务声明的平台白名单之外的调用一律拒绝。

## 构建溯源

每次产出物的门禁都会自动留下一条可验证记录，不需要开发者额外填写：

| 记录内容 | 来源 |
|---|---|
| 产物指纹与大小 | 门禁收集产物时计算 |
| 源码版本 | 构建时工作区的 HEAD |
| 构建命令 | 门禁实际执行的 argv |
| 构建环境 | 执行画像、虚拟机、内核与运行时 |
| 依赖清单 | 从工作区的 requirements / package.json / build.gradle / go.mod 读取 |
| 构建人与时间 | 认领任务的 agent 与记录时间 |

记录带指纹和 Ed25519 签名，并写入审计链。任何人拿到构建编号就能查：

```bash
curl -H "Authorization: Bearer $TOKEN" https://<host>/v1/provenance/B-20261116-56C782
curl https://<host>/verify/B-20261116-56C782          # 公开验证页
curl https://<host>/verify/B-20261116-56C782/qr.svg   # 贴在产物上的二维码
```

验证会重算三件事：记录指纹、签名、产物文件的 SHA-256。替换产物或改动任意字段，
验证页立即变红，并指出是哪一项不通过。

## 可观测与运维

- `GET /v1/health`、`GET /metrics`（Prometheus 文本格式）
- 观察者心跳 + 事件新鲜度，外部 watchdog 每分钟巡检，异常自动重启
- 每日备份（数据库 + 仓库 bundle + 工作区）、WAL 归档、PITR 演练、恢复演练
- 发布门禁 `./deploy/release-check.sh`：健康、审计链、复制、单元、磁盘、
  三条真实构建链、平台路由、构建溯源、浏览器 E2E

## 配置

所有部署相关的地址、路径、外部服务都在 `config/macp.toml`（模板
`config/macp.example.toml`）。改动后：

```bash
python3 deploy/render_env.py        # 渲染 deploy/.env
python3 -m server.settings doctor   # 校验 + 探测已启用的集成
```

缺配置也能跑：默认单机、只监听 loopback、不连任何外部设备。详见 [docs/CONFIG.md](docs/CONFIG.md)。

## 文档

| 主题 | 位置 |
|---|---|
| 配置项与拆分部署 | [docs/CONFIG.md](docs/CONFIG.md) |
| 代码导读（新手向） | [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) |
| MCP 网关 | [docs/MCP.md](docs/MCP.md) |
| 联盟链锚定 | [deploy/fabric/README.md](deploy/fabric/README.md) |
| 上链演示手册（课设） | [docs/FABRIC-DEMO.md](docs/FABRIC-DEMO.md) |
| 项目官网 | [site/README.md](site/README.md) |
| 最小审计版 | [deploy/minimal/README.md](deploy/minimal/README.md) |

## 许可

MIT，见 [LICENSE](LICENSE)。
