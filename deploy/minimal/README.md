# 最小审计版

一台机器、一个端口、十一个容器。它保留了审计闭环，砍掉了与审计无关的部分。

在 2 核 / 7.6 GB 的机器上实测：

| 指标 | 数值 |
|---|---|
| 容器 | 11 个（全部复用已有 `macp-api` 镜像，不额外构建） |
| 内存 | **297 MiB** 合计 |
| 磁盘 | 数据卷 49 MB（空库） |
| 端口 | 只监听 `127.0.0.1:8280` |
| 启动到健康 | 约 30 秒 |

## 跑起来

```bash
./deploy/bootstrap.sh          # 完整版准备密钥与 agent 公钥（一次性）
./deploy/minimal/up.sh         # 起 11 个容器并自检
./deploy/minimal/smoke.sh      # 跑一条完整审计闭环，逐项断言
```

浏览器打开 `http://127.0.0.1:8280`，用 `deploy/secrets/write_token` 登录。

```bash
./deploy/minimal/up.sh --down    # 停掉，保留数据
./deploy/minimal/up.sh --reset   # 停掉并清空数据
```

## 保留了什么

| 组件 | 作用 |
|---|---|
| `api` | 任务图、工具网关、回执、审计查询、控制台 |
| `signer` | Ed25519 回执签名，私钥只在这里 |
| `runner` | bwrap 沙箱执行（`local` 画像 / `sandbox` 平台） |
| `observer` | 事件链推进与告警 |
| `scheduler` / `coordinator` / `merge-worker` | 派单、阻塞升级、单车道合并 |
| `worker-e` + `mock-openai` | 一个 worker 与内置模型，不需要真实 API key |
| `nginx` | 单一 HTTP 入口 |
| `db` | PostgreSQL 16，启动即建表 |

## 砍掉了什么

| 去掉 | 原因 |
|---|---|
| `api-2`、`oidc` | 单实例即可；登录用管理员 token，不跑 SSO |
| `searxng` | 不联网检索 |
| KVM 构建节点、Windows 构建机 | 只有 sandbox 画像；需要 Linux/Android/Windows 真实产物时，把 `[kvm].node_url` 指到构建节点即可，容器数不变 |
| Fabric 桥接 | 审计链保留为本地哈希链；联盟链锚定在完整版里 |
| 备份/WAL/演练定时器 | 属于生产运维，不在演示范围 |

## 自证脚本证明什么

`smoke.sh` 跑完一条真实闭环并逐项断言：

```
PASS 空闲 worker | worker-deepseek
PASS 任务创建 | T-948272377
PASS 派单 | worker-deepseek
PASS 认领并拿到任务令牌 | len=48
PASS 受控写入 | src/main.py
PASS 工作区提交 | 4b3af073
PASS 门禁执行 | compile exit=0
PASS 门禁留证 | 427d1297
PASS 候选 ref | refs/candidates/T-948272377/1
PASS 回执签发 | R-719447169
PASS 回执验签 | ed25519 通过
PASS 审计链完整 | 16 条事件
PASS 任务状态 | review
```

顺序有个坑：**先提交、再跑门禁**。门禁会把 `.evidence/` 写进工作区，
如果先跑门禁再提交，`workspace_commit` 会因为证据文件越出写面而回滚成空提交。

## 两个已知差异

- 控制台里的 SSO 登录按钮在最小版不可用（没有 oidc 服务），用管理员 token 登录即可；
- 没有构建节点时不会产生产物溯源记录（构建指纹、二维码、验证页），
  因为溯源的对象是构建产物本身。
