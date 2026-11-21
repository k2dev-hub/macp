# 演示任务书：一次可审计的交付

这份任务书用于现场演示：从建单到上链，一条任务走完平台的完整链路。
工单已在真实部署上跑通，本文末尾附有本次实测产生的编号，可对照。

---

## 一、任务目标

在示例仓库中新增一个纯函数与其测试，并产出一个可复现的构建产物。

| 项 | 内容 |
|---|---|
| 任务标题 | 为示例应用实现 multiply：单测 + 集成测试 + 构建产物 + 上链存证 |
| 任务编号 | `T-9003`（必须是 `T-` + 纯数字，至少 4 位） |
| 工作流 | `stream-core` |
| 类型 | `slice` |
| 风险级别 | L1（改 `src/**`、`tests/**`，不触及高风险路径） |
| 执行画像 | `kvm-debian`（真实 Debian 虚拟机） |
| 平台白名单 | `sandbox`、`linux` |

**为什么这样设计**：四个门禁覆盖"语法能过 → 单测能过 → 模块能联 → 真机能构建"，
且 `build` 必须在虚拟机里跑，才能证明"门禁绑定执行环境"这条设计原则。

---

## 二、工单（可直接 POST）

```json
{
  "code": "T-9003",
  "title": "为示例应用实现 multiply：单测 + 集成测试 + 构建产物 + 上链存证",
  "workstream": "stream-core",
  "kind": "slice",
  "priority": "P1",
  "work_order": {
    "objective": "在示例仓库中实现 multiply(a,b)，补齐单元测试、集成测试与 Makefile，使 compile/unit/integration/build 四个门禁全部通过。",
    "write_surface": ["src/**", "tests/**", "Makefile", "notes/**"],
    "constraints": [
      "只允许改动 write_surface 内的路径",
      "src/app.py 保留现有 add(a,b) 不动",
      "Makefile 的默认目标必须产出 dist/hello.txt",
      "tests/integration/ 下至少一个集成测试"
    ],
    "acceptance": [
      {"gate": "compile", "level": "L1", "required": true},
      {"gate": "unit", "level": "L1", "required": true},
      {"gate": "integration", "level": "L1", "required": true},
      {"gate": "build", "level": "L1", "required": true}
    ],
    "execution": { "profile": "kvm-debian", "platforms": ["sandbox", "linux"], "collect": ["dist/**"] },
    "completion_contract": {
      "observable": "新增 src/app.py:multiply、tests/test_app.py、tests/integration/test_workflow.py、Makefile，dist/hello.txt 可复现",
      "entrypoints": ["src/app.py:multiply"],
      "consumers": ["tests/test_app.py", "tests/integration/test_workflow.py"]
    }
  }
}
```

提交：

```bash
curl -sk -X POST https://<host>:9443/v1/tasks \
  -H "Authorization: Bearer $(cat deploy/secrets/write_token)" \
  -H "Content-Type: application/json" \
  --data-binary @demo-task.json
```

---

## 三、四个门禁各做什么

| 门禁 | 命令（服务端固定） | 通过标准 | 产出 |
|---|---|---|---|
| `compile` | `python3 -m compileall -q .` | 退出码 0 | 证据日志 |
| `unit` | `python3 -m unittest discover -s tests -v` | 全部用例通过 | 证据日志 + 用例数 |
| `integration` | `python3 -m unittest discover -s tests/integration -v` | 全部用例通过 | 证据日志 |
| `build` | `make`（默认目标） | 退出码 0 且产出 `dist/**` | **构建溯源记录 + 链上锚定** |

只有 `build` 会生成溯源记录：因为它产出的是**产物**，其余门禁产出的是**判断**。

---

## 四、演示动线（约 8 分钟）

| 时间 | 操作 | 说的话 |
|---|---|---|
| 0:00 | 打开 **指南** 页 | "四步：配模型、建项目、派单、看审计" |
| 0:40 | 打开 **任务**，点「新建任务」 | "工单里写死三件事：能改哪些文件、必须过哪些门禁、在什么环境跑" |
| 1:30 | 派单给 `worker-deepseek` | "调度器把任务派给空闲 Agent，Agent 拿到的是绑定任务与尝试的令牌" |
| 2:00 | 切 **审计 · 事件流** | "它在写文件、跑命令、过门禁，每一步都在滚" |
| 4:00 | 回 **任务** 看门禁结果 | "四个门禁全绿，每个都有证据编号" |
| 5:00 | 打开 **产物** → 构建溯源 | "这次构建的产物 `hello.txt`，16 字节，指纹可复算" |
| 5:40 | 点「验证页」 | "扫码就能验：指纹、签名、产物哈希三项" |
| 6:30 | 回 **审计 · 上链** | "这条指纹已经钉在 Hyperledger Fabric 上，block 33" |
| 7:30 | 收尾 | "整条链没有一处需要相信某个人，只相信数学" |

---

## 五、本次实测结果（2026-11-21）

```
任务        T-9003            state: accepted
回执        R-575544099       status: accepted，验签 valid: true
候选分支    refs/candidates/T-9003/1
构建记录    B-20261121-201C48
链上交易    d8e409c1a719...   block 33，anchored_at 2026-11-21T00:22:23Z
链上比对    指纹一致（fingerprint_matches: true）
验证页      /verify/B-20261121-201C48  →  HTTP 200
产物        hello.txt / 16 B / sha256 6bb4930e...
```

---

## 六、现场两个必知点

**1. 任务编号必须纯数字。** `T-DEMO01` 会触发 `tasks_code_check` 约束，返回 500。
格式是 `T-` 加 4–8 位数字，例如 `T-9003`。

**2. 写面含 `src/**` 会被自动要求集成测试。** 平台按变更风险定级（L2），
自动追加 `unit` 与 `integration` 两个必过门禁。所以工单里要显式写上
`tests/integration/` 的约束，否则回执会一直缺项、任务卡在 `blocked`。

---

## 七、备用方案

如果现场模型调用不稳（网络或供应商限流），有两条退路：

- **换成 mock 模型**：`worker-llm` 接的是内置 mock-openai，不依赖外网，行为确定。
- **只演示既有交付**：任务 `T-9003` 与 `B-20261121-201C48` 已经存在，
  直接打开验证页与上链页讲解，不需要再跑一次。

---

## 八、这份任务书之外，当天修掉的两个缺陷

演示准备过程中发现并修复，均已上线：

1. **门禁调错解释器**：`compile`/`unit` 原先用容器内的 `sys.executable`（`python3.12`），
   而 KVM 节点白名单只有 `python3`，导致两个门禁永远 422 `argv_not_allowed`。已改为显式调用 `python3`。
2. **集成门禁被忽略**：写面含 `src/**` 即判为 L2 风险，平台自动要求 `unit` + `integration`；
   若工单没写集成测试约束，回执会缺项、任务被 Agent 主动阻塞（这是正确行为，不是 bug，但容易踩）。
