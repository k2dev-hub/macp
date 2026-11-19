# 上链演示手册（课设用）

本文档面向"把上链讲清楚"的场合：课堂演示、答辩、验收。内容包括演示流程、
每一幕要说的话、技术设计要点，以及评委最可能追问的问题与回答。

一句话定位：**链下留证据，链上钉指纹，任何人可复核。**

---

## 1. 先理解三条线

```
① 链下（PostgreSQL）          ② 链上（Fabric）              ③ 验证
   build_provenance 记录         链码 provenance               /verify/<build_id>
   ├─ 产物 SHA-256               ├─ anchor(build_id, 指纹)     ├─ 重算记录指纹
   ├─ 源码提交 / 构建命令         ├─ query / history            ├─ 验 Ed25519 签名
   ├─ 环境 / 依赖清单             ├─ recent / stats             ├─ 重算产物哈希
   └─ Ed25519 签名                └─ 每笔一笔交易、进区块        └─ 与链上指纹对照
```

三条线的关系：①是主数据，②只保存**指纹**，③是任何人都能执行的自证。
链上不存源码和产物，不是因为做不到，而是**隐私与成本**：指纹不可逆，
足以证明"没被改"，而源码不该上链。

---

## 2. 演示前 5 分钟检查

```bash
# 1) 链网络在跑
docker ps --filter name=macp-fabric --format "{{.Names}} {{.Status}}"
#    期望：macp-fabric-orderer / -peer / -cli 三个 Up

# 2) 桥接在跑
systemctl is-active macp-fabric-bridge
curl -s http://172.28.0.1:9310/health

# 3) 宿主机防火墙放行两个 docker 网段（172.28 MACP / 172.19 Fabric）
#    重启后 dockerd 会把 bridge-nf 改回 1，这条命令要再跑一次
sh deploy/hostfw/docker-allow.sh

# 4) 链码版本（本次演示需要 recent/stats，即 1.1）
docker exec macp-fabric-cli peer lifecycle chaincode querycommitted \
  --channelID macp --name provenance | grep -E "Version|Sequence"

# 5) 主栈健康
curl -sk https://localhost:9443/v1/health
```

如果第 4 步显示的版本低于 1.1（没有 `recent`/`stats`），升级一次：

```bash
cd deploy/fabric && ./upgrade-cc.sh
```

升级完成后 `stats` 应能返回 `{"total": ...}`。

---

## 3. 演示脚本（约 8 分钟）

### 第 1 幕 · 现状（1 分钟）

```bash
curl -s -H "Authorization: Bearer $(cat deploy/secrets/fabric_bridge_token)" \
  http://172.28.0.1:9310/stats
```

**说**：链上现在有多少条锚定、时间跨度是多少。这是"账本"的体量，
不是数据库的体量——两个数字不必相等，下面会解释为什么。

**看**：`total`、`first_anchored`、`last_anchored`。

### 第 2 幕 · 一次真实构建（3 分钟）

```bash
python3 deploy/verify/verify-provenance.py
```

这条命令会依次做：建任务 → 派单 → 认领 → 写文件 → 提交 → **在 Debian 虚拟机里真实构建**
→ 生成溯源记录 → 上链锚定 → 读回校验 → 篡改产物 → 验证失败 → 还原 → 再次通过。

**说**：注意每一步的 PASS，尤其这三行：

```
PASS gate produced a provenance record | B-2026...
PASS anchored on fabric | anchored
PASS chain returns the same fingerprint | 200 local=xxxx chain=xxxx
```

**看**：终端里 `local=` 与 `chain=` 两个前缀相同，即"链上链下一致"。

### 第 3 幕 · 链上账本（1.5 分钟）

```bash
curl -sk -H "Authorization: Bearer $(cat deploy/secrets/write_token)" \
  "https://localhost:9443/v1/fabric/ledger?limit=5"
```

或直接打开页面：**`https://localhost:9443/chain`**

**说**：页面把"链上最近 50 条"与"本地数据库"逐条对照。四种状态：

| 状态 | 含义 | 是否正常 |
|---|---|---|
| `consistent` | 两边指纹相同 | 正常 |
| `chain-only` | 链上有、本地已无（数据库重置或归档过） | 正常，链只增不删 |
| `local-only` | 本地有、从未锚定（链不可达时发生过） | 需要补锚 |
| `mismatch` | 两边指纹不同 | 永远不应出现 |

演示环境里数据库被重置过，所以会看到一批 `chain-only`——这恰好可以讲
"链上凭证比数据库活得更久"。

### 第 4 幕 · 全量巡检（1 分钟）

```bash
python3 deploy/fabric/verify-ledger.py
```

**说**：这不是"演示脚本"，而是可以放进 CI 的检查。它退出码非 0 就代表存在不一致，
适合定时跑。

### 第 5 幕 · 给评委看的证据页（1 分钟）

打开 `https://localhost:9443/verify/<build_id>`（第 2 幕打印过），或扫产物旁的二维码。

**说**：这一页是给"买家"看的：产物指纹、构建人、源码版本、联盟链交易号，
以及三项校验（记录指纹 / 签名 / 产物哈希）的结果。它是公开的、只读的，
不需要登录，也不泄露源码。

---

## 4. 技术设计：五个关键决定

### 4.1 只上链指纹，不上链文件

- 链上数据对所有参与方可见，源码与产物属于敏感资产；
- 链上存储昂贵，几百 KB 的产物乘以构建次数不可接受；
- 指纹（SHA-256）不可逆，**足以证明"同一个东西"**，这正是溯源需要的。

因此：产物留在工作区/对象存储，链上只写 `指纹 + 构建编号 + 构建人 + 时间`。

### 4.2 锚定是"附加步骤"，失败不影响主流程

记录先落数据库，再尝试上链；上链结果写进 `fabric_status`（`anchored` / `failed`）。
链不可达时，构建照常完成，只是这一条没有链上凭证——巡检会把它标成 `local-only`。
这是一种有意识的降级：**审计链不能因为外部网络故障而中断**。

### 4.3 链码保证幂等，异指纹拒绝

```go
已有记录 && 指纹相同 → 直接返回原记录（幂等）
已有记录 && 指纹不同 → 返回错误，拒绝写入
```

为什么重要：网络重试、桥接重放都可能重复提交同一笔锚定。幂等让重试安全；
而异指纹拒绝则堵死"用新记录覆盖旧记录"的洗白路径。

### 4.4 桥接隔离：应用容器不碰私钥

```
MACP API  ──HTTP──▶  fabric-bridge (:9310)  ──peer CLI──▶  peer/orderer
                        └── 只有这里持有管理员身份与 MSP 材料
```

好处：
- API 容器里没有 Fabric SDK、gRPC、证书，被攻破也拿不到链上身份；
- 换通道、换组织、升级链码，只动 bridge，不动业务代码；
- 桥不可用就是"未锚定"，不会把 API 拖垮。

### 4.5 三道防线，篡改无处可藏

| 层次 | 手段 | 发现什么 |
|---|---|---|
| 记录级 | 规范化 JSON 的 SHA-256 指纹 | 记录字段被改 |
| 签名级 | Ed25519（signer 独立容器） | 伪造记录（无密钥签不出来） |
| 产物级 | 重新计算文件哈希 | 产物被替换 |
| 外部级 | 链上锚定 | 整库被重写（链上仍留原始指纹） |

前三条在 `/verify/<id>` 页面上一目了然；第四条需要联盟链里另一个组织配合才能伪造。

---

## 5. 评委可能追问的 12 个问题

**Q1：为什么不用以太坊？**
联盟链面向已知参与方，有准入控制（MSP）、无代币、吞吐更高、数据默认不公开。
Fabric 的通道机制正好对应"多家单位共管一份账本"。

**Q2：单组织网络也算联盟链吗？**
当前是单组织验证网络，用于证明链路与数据格式。联盟链的"多方"体现在通道策略与
多组织 peer：增加 Org2 只需新增一套 crypto 与 peer，并更新通道策略，
**链码与业务代码不需要改**。这是可扩展性，不是缺失。

**Q3：链上链下不一致怎么办？**
两层：锚定时 bridge 立即读回比对（不一致直接判失败）；`verify-ledger.py`
做全量巡检，标出 `local-only` / `chain-only` / `mismatch`。

**Q4：数据库被整体重写，还能发现吗？**
能，只要链上指纹还在。重写后的记录指纹与链上不符，巡检即报 `mismatch`。
本地事件链的每日锚点则用于发现"链下历史被改写"。

**Q5：链上数据能删吗？**
不能，只能追加。这正是溯源需要的不变性。本地记录可以归档，但链上凭证保留。

**Q6：上链很慢吗？会影响构建吗？**
锚定在构建完成后异步附加，一次约 1–2 秒（orderer 批处理 2 秒）。
它不在构建的关键路径上，慢也不会拖慢构建。

**Q7：为什么指纹要 64 位十六进制？**
SHA-256 的十六进制表示。链码会校验长度，防止把别的字段误传成指纹。

**Q8：签名和上链不是重复了吗？**
不重复：签名证明"谁签的"，上链证明"某个时刻已存在且未被改"。
签名可以被伪造者用自己的密钥重签，链上时间戳与多份副本则不能。

**Q9：私有数据能放链上吗？**
不能，也不放。链上只有指纹与最小元数据（构建编号、构建人、时间），
项目名与任务号是否上链由配置决定。

**Q10：怎么证明链上那条就是这次构建？**
链上记录的指纹 = 本地记录指纹 = 页面重算的指纹，三者一致；
构建编号是主键，交易号可反查。

**Q11：如果 orderer 挂了怎么办？**
构建不受影响，锚定失败并记为 `failed`；恢复后重跑一次构建或补锚即可。
演示中可以先停 orderer 展示降级，再启回来展示恢复。

**Q12：这套东西能用到真实项目吗？**
可以，但要补：多组织部署、链码升级流程（本仓库有 `upgrade-cc.sh`）、
节点监控、以及"锚定失败的重试队列"。当前实现已经具备幂等与降级，
缺的是运维层面的自动化。

---

## 6. 现场应急

| 现象 | 处理 |
|---|---|
| `/chain` 显示"链不可达" | `systemctl restart macp-fabric-bridge`；再 `curl :9310/health` |
| `stats` 报 `unknown function` | 链码还是 1.0，跑 `./upgrade-cc.sh` |
| 记录 `fabric_status=failed` | 链恢复后重跑 `verify-provenance.py`，或对该条重新 `POST /anchor` |
| 验证页打不开 | 确认 MACP 主栈：`docker ps | grep macp-api`，再 `curl -k /v1/health` |
| `context deadline exceeded` | 宿主防火墙没放行 172.19.0.0/16：跑 `sh deploy/hostfw/docker-allow.sh` |
| `fabric_tx_id` 与指纹相同 | 旧版桥接的已知缺陷，升级到含读回重试的版本即可 |
| 演示机断网 | 单机演示不需要外网；只有 `[fabric]` 指向外部链时才需要 |

---

## 7. 一句话收尾

> 这套系统的上链部分只做一件事：**把"构建产物的指纹"在某个时刻钉在多方共有的账本上**。
> 它不追求把整个构建过程搬上链，而是用最小的链上数据换取最强的可证明性。
