# 配置与解耦（`config/macp.toml`）

MACP 的运行时不认识任何具体机器：地址、NAS 路径、构建节点、外部服务全部来自
`config/macp.toml`。仓库里只提交模板 `config/macp.example.toml`；真实配置被
`.gitignore` 忽略，因为它绑定的是**这套部署**的地址与路径。

```bash
cp config/macp.example.toml config/macp.toml   # 然后按实际环境编辑
python3 deploy/render_env.py                   # 生成 deploy/.env（compose 用）
python3 -m server.settings doctor              # 校验 + 探测已启用的集成
```

## 1. 设计约定

| 约定 | 含义 |
|---|---|
| 缺文件也能跑 | 没有 `config/macp.toml` 时用内置默认值：单机、只监听 loopback、不连任何外部设备 |
| 集成默认关闭 | `enabled = false` 或地址留空 = **跳过**该集成，而不是启动失败 |
| 环境变量优先 | `MACP_<SECTION>__<KEY>` 覆盖文件值（容器里就是这么注入的） |
| 只存路径不存秘密 | 配置里只有 `*_file` 路径；token/密钥文件权限 0600，永不打印 |
| 一处生效 | 改配置 → `render_env.py` → 重建受影响的服务；脚本每次运行都重新读文件 |

## 2. 各段说明

### `[deployment]`

| 键 | 说明 |
|---|---|
| `public_url` | 客户端访问的地址（浏览器、MCP、OIDC 回调）。放反向代理后面时填外部 URL |
| `root` | 安装目录；脚本一般按自身路径推断，这里只给 systemd/cron 兜底 |
| `state_dir` | 不放进 Docker volume 的可写状态（锁、演练文件、本地备份） |
| `timezone` | 报告/时间戳用的时区 |

### `[api]`

`internal_base`（容器网内 API 地址）、`publish_address`/`publish_port`（nginx
发布地址，默认 `127.0.0.1:9443`）、`proxy_read_timeout`（VM 门禁要长）。

### `[database]`

`host`（容器名或外部主机）、端口、库名、账号，`standby_hosts` 给故障转移用。
留空 `standby_hosts` 就关闭自动提升。

### `[gitea]`

**可选**。只用于拉取/发布平台源码本身；平台运行不依赖它（仓库由工作区里的
bare repo 提供）。`enabled=true` 时填 `url`、`repository`、`branch`，token 放
`token_file` 指向的文件。

### `[backup]` / `[wal]` / `[audit]`

| 键 | 说明 |
|---|---|
| `backup.enabled` + `backup.root` | 共享存储挂载点；为空则只保留本地备份 |
| `backup.archive_dir` / `wal_mirror_dir` | 归档目录（相对 `root`） |
| `backup.retention_days` / `keep_local` | 远端保留天数、本地保留份数 |
| `wal.archive_dir` | db 容器内看到的 WAL 归档目录 |
| `wal.host_archive_dir` | 宿主看到的同一目录（脚本用） |
| `wal.keep_hours` / `keep_recent` / `min_size_bytes` | 清理策略的护栏 |
| `audit.anchor_dir` / `events_archive_dir` | 哈希链锚点与事件归档；**留空 = 落在 `state_dir` 下**，无需外部存储 |

没有 NAS 时这些脚本会打印 `backup disabled ...` 并跳过远端步骤，本地部分照常。

### `[kvm]`

**可选**。`enabled=false` 时只剩 `sandbox`/`local` 执行画像。启用后填
`node_url`（构建节点 HTTP 地址）、`node_host`/`node_user`（运维 SSH）、
`image_dir`（黄金镜像所在目录）、`windows_domain`/`windows_workdir`（常驻
Windows 构建机）。

### `[signer]` / `[oidc]` / `[runner]` / `[mcp]`

- `signer`：Ed25519 回执签名服务与密钥目录；关闭时回执会被拒绝（不会写占位签名）。
- `oidc`：`issuer` 留空则用 `public_url + /oidc`（内置 IdP）；接外部 IdP 时填
  `issuer` 并改 `redirect_uris`。
- `runner`：bwrap 沙箱（`sandbox`/`local` 画像）。
- `mcp`：MCP 网关的会话 TTL、并发上限、限流与输出截断。

### `[observability]` / `[e2e]`

磁盘/WAL 告警阈值、watchdog 开关；浏览器 E2E 用的浏览器与截图目录。

## 3. 校验：`doctor`

```bash
$ python3 -m server.settings doctor
config        ok           /opt/macp/config/macp.toml
public_url    ok           https://macp.example.com:9443
database      ok           container macp-db-standby
signer        configured   http://signer:9300 -> container running healthy
runner        configured   http://runner:9200 -> container running
kvm           configured   http://build-host:9300 -> reachable (HTTP 200)
backup        ok           /mnt/nas
gitea         disabled     source is managed manually / by the local repo
signer keys   ok           /opt/macp/deploy/agent_keys
public keys   ok           /opt/macp/deploy/agent_pubkeys

doctor: OK
```

- 只探测**已启用**的集成；容器名通过 `docker inspect` 判断健康状态，
  外部地址走 HTTP；
- 任一集成 `down/missing/unwritable` 时退出码为 1，适合放进发布检查；
- `python3 -m server.settings` 打印全部生效值，`... json` 输出 JSON。

## 4. 从一台机器迁到另一台

1. 拷贝仓库（不含 `config/`、`deploy/secrets/`、`deploy/tls/`）；
2. 写新的 `config/macp.toml`（改 `public_url`、`publish_address`、NAS 路径、
   KVM 节点）；
3. `python3 deploy/render_env.py`；
4. 重新生成 TLS（`deploy/gen_tls.sh`，SAN 取自 `public_url`）或放入自己的证书；
5. 恢复 `deploy/secrets/`（token、签名密钥、OIDC 密钥）；
6. `python3 -m server.settings doctor` 全绿后 `docker compose up -d`。

## 5. 仍未配置化（已知边界）

| 位置 | 说明 |
|---|---|
| `deploy/docker-compose.yml` 的服务名/卷名 | 属于栈内拓扑，不是设备绑定；改它们要同步 `nginx.conf` 的 upstream |
| `deploy/kvm-node/*.sh` 里的镜像构建细节 | 需要 libvirt/网络参数，属于构建节点自身的部署脚本 |
| `docs/` 里历史运维记录 | 按当时的机器写的，保留作记录；通用步骤以本文件为准 |

## 6. 拆分部署：应用和数据库不在一台机器

数据库可以留在原来的机器上，只把应用层搬到另一台。这套部署里就是这么做的
（`.11` 跑应用，`.10` 只跑 PostgreSQL）。

```
客户端 ──https──▶ .11 nginx ──▶ api / mcp / worker…
                                    │
                                    └─ 172.28.0.1:15432 ──SSH 隧道──▶ .10:127.0.0.1:15432 ──▶ PostgreSQL
```

要点：

| 事项 | 做法 |
|---|---|
| 网络 | compose 网络固定子网 `172.28.0.0/16`；宿主机上开 SSH 隧道把 `172.28.0.1:15432` 转发到数据库机器的 loopback。数据库侧不对外暴露端口 |
| 配置 | `[database] host = "172.28.0.1"`, `port = 15432`；`deploy/render_env.py` 会写进 `DATABASE_URL` |
| 备份 | 在应用机跑 `deploy/backup.sh`：检测到本地没有 db 容器时，改用 `postgres:16-alpine` 客户端容器经隧道 `pg_dump` |
| watchdog | 同上，用客户端容器读观察者新鲜度 |
| 数据库侧脚本 | `anchor_events.sh`、`wal_*`、`retention.sh`、`pitr/restore_drill.sh`、`pg_failover.sh` 仍留在数据库机器上，它们 `docker exec` 本地容器 |
| 定时器 | 应用机装 `install_watchdog_timer.sh` / `install_autoheal_timer.sh` / `install_backup_timer.sh`；数据库机器保留 `wal` / `anchor` / `retention` / `drill` / `failover` |

主机如果是 docker + libvirt 共存，注意两点（都会表现为“容器之间通、容器出不去”）：

```bash
# 1) 自定义 nftables 防火墙默认 DROP 时，放行 docker 子网
nft add rule inet hostfw input ip saddr 172.28.0.0/16 accept
nft add rule inet hostfw forward ip saddr 172.28.0.0/16 accept
nft add rule inet hostfw forward ip daddr 172.28.0.0/16 accept

# 2) 桥接流量不要重复过 iptables（否则容器互访被 FORWARD 链丢掉）
sysctl -w net.bridge.bridge-nf-call-iptables=0
```
