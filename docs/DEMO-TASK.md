# 演示任务书：让 Agent 造一个能自证的校验器

这条任务让 Agent 在平台内交付一个**真能用的小工具**，然后用这个工具去验证平台自己签发的溯源记录。
一条任务同时体现：工单约束、四个门禁、真实虚拟机构建、签名回执、构建溯源、联盟链锚定。

工单已在真实部署上跑通，文末附本次实测编号。

---

## 一、任务目标

实现 `macp-verify`：一个只依赖 Python 标准库的离线校验器。
给定「溯源记录 JSON」与「产物文件」，重新计算产物 SHA-256，与记录中的值比对，
输出 PASS/FAIL 并用退出码表达结果。

| 项 | 内容 |
|---|---|
| 任务编号 | `T-9101`（`T-` + 纯数字，至少 4 位） |
| 工作流 | `stream-core` |
| 类型 | `slice` |
| 执行画像 | `kvm-debian`（真实 Debian 虚拟机） |
| 平台白名单 | `sandbox`、`linux` |
| 交付物 | `dist/macp-verify.py`（可执行，带 shebang） |

**为什么选这个工具**：它不是玩具。它是"验证平台自身记录"的那把尺子——
演示时可以直接说：「这条记录是平台签的，这个校验器是平台自己造出来的，现在让它验一下。」

---

## 二、CLI 契约（写进工单，模型照此实现）

```
python3 macp-verify.py --record <provenance.json> --artifact <file>
```

| 情形 | 输出 | 退出码 |
|---|---|---|
| 哈希一致 | 打印 build_id、记录里的 sha256、实际 sha256，末行 `PASS` | 0 |
| 哈希不一致 | 同上三行，末行 `FAIL` | 1 |
| 参数缺失或文件不存在 | 错误说明 | 2 |

约束：不得引入第三方依赖；只能读文件，不改文件。

---

## 三、工单（可直接 POST）

```json
{
  "code": "T-9101",
  "title": "实现 macp-verify：离线校验产物与溯源记录",
  "workstream": "stream-core",
  "kind": "slice",
  "priority": "P1",
  "work_order": {
    "objective": "实现一个只依赖 Python 标准库的命令行工具 macp-verify：读取溯源记录 JSON 与产物文件，重新计算产物 SHA-256 并与记录比对，输出 PASS/FAIL 且用退出码表达结果。",
    "write_surface": ["src/**", "tests/**", "Makefile", "notes/**"],
    "constraints": [
      "只允许改动 write_surface 内的路径",
      "不得引入第三方依赖，只能用 Python 标准库",
      "CLI 必须支持 --record <path> 与 --artifact <path> 两个参数",
      "匹配时退出码 0 并打印 PASS；不匹配时退出码 1 并打印 FAIL；参数或文件缺失时退出码 2",
      "输出必须包含三行：记录里的 build_id、记录里的 artifact_sha256、实际计算的 sha256",
      "Makefile 的默认目标是第一个 target，必须产出 dist/macp-verify.py（可执行，带 shebang）"
    ],
    "acceptance": [
      {"gate": "compile", "level": "L1", "required": true},
      {"gate": "unit", "level": "L1", "required": true},
      {"gate": "integration", "level": "L1", "required": true},
      {"gate": "build", "level": "L1", "required": true}
    ],
    "execution": { "profile": "kvm-debian", "platforms": ["sandbox", "linux"], "collect": ["dist/**"] },
    "completion_contract": {
      "observable": "dist/macp-verify.py 可执行：对一致的文件返回 0 并打印 PASS，对被替换的文件返回 1 并打印 FAIL",
      "entrypoints": ["src/macp_verify.py:main"],
      "consumers": ["tests/test_macp_verify.py", "tests/integration/test_cli.py"]
    }
  }
}
```

---

## 四、四个门禁

| 门禁 | 服务端命令 | 通过标准 | 产出 |
|---|---|---|---|
| `compile` | `python3 -m compileall -q .` | 退出码 0 | 证据日志 |
| `unit` | `python3 -m unittest discover -s tests -v` | 全部通过 | 证据 + 用例数 |
| `integration` | `python3 -m unittest discover -s tests/integration -v` | 全部通过 | 证据 + 用例数 |
| `build` | `make`（默认目标） | 退出码 0 且产出 `dist/**` | **溯源记录 + 链上锚定** |

只有 `build` 生成溯源记录——因为它产出的是**产物**，其余门禁产出的是**判断**。

---

## 五、演示动线（约 8 分钟）

| 时间 | 操作 | 讲解要点 |
|---|---|---|
| 0:00 | 打开 **指南** | 四步：配模型 → 建项目 → 派单 → 看审计 |
| 0:40 | **任务** → 新建（粘贴工单） | 工单写死三件事：能改哪些文件、必过哪些门禁、在什么环境跑 |
| 1:30 | 派单给 `worker-deepseek` | Agent 拿到的是绑定任务与尝试的令牌，不是一把万能钥匙 |
| 2:00 | **审计 · 事件流** | 写文件、跑命令、过门禁，逐步滚出来 |
| 4:00 | 回 **任务** 看门禁 | 四个门禁全绿，每条都有证据编号 |
| 5:00 | **产物** → 构建溯源 | 产物 `macp-verify.py`，5914 字节，可下载 |
| 5:40 | 点 **验证页** | 指纹、签名、产物哈希三项校验 |
| 6:20 | **审计 · 上链** | 这条指纹已钉在 Hyperledger Fabric，block 50 |
| 7:00 | 终端里跑一次校验器 | **PASS exit 0**；改一个字节后 **FAIL exit 1** |
| 7:50 | 收尾 | "验证这件事，不需要相信任何人" |

---

## 六、本次实测结果（2026-11-21）

```
任务        T-9101            state: accepted
回执        R-731273713       status: accepted，验签 valid: true
构建记录    B-20261121-D682F9
产物        macp-verify.py / 5914 B / sha256 a2a66272…
链上交易    8f37cf8304dc…     block 50，指纹比对一致
验证页      /verify/B-20261121-D682F9 → HTTP 200
```

工具实测（在部署机上直接跑）：

```
$ python3 macp-verify.py --record provenance.json --artifact macp-verify.py
build_id: B-20261121-D682F9
artifact_sha256: a2a662725b9be65557401884f6497d22f4e6a07be1adea4e52b809c5a09da131
actual_sha256:   a2a662725b9be65557401884f6497d22f4e6a07be1adea4e52b809c5a09da131
PASS
exit=0

$ python3 macp-verify.py --record provenance.json --artifact tampered.py
artifact_sha256: a2a662725b9be65557401884f6497d22f4e6a07be1adea4e52b809c5a09da131
actual_sha256:   b4a36b35c80f780913f512fc7c930d3b979b6f95a03c897caff790fb2b195b1b
FAIL
exit=1
```

---

## 七、现场三个必知点

**1. 任务编号必须纯数字。** `T-DEMO01` 触发 `tasks_code_check`，接口 500。

**2. 写面含 `src/**` 会被判 L2 风险**，平台自动要求 `unit` 与 `integration` 两个门禁；
工单里必须显式写上，否则回执缺项、任务被 Agent 主动阻塞。

**3. 回执里的门禁项必须有退出码。** 写 `status: "passed"` 现在也能通过（平台已兼容），
但最稳的写法是 `{"gate": "compile", "exit": 0, "evidence_id": "..."}`。

---

## 八、备用方案

- 模型调用不稳：改派给 `worker-llm`（内置 mock，不依赖外网）。
- 不想重跑：直接讲 `T-9101` 这一单——回执、溯源、链上记录、验证页都还在。
- 只讲工具：在部署机 `/tmp/mv` 下已有 `macp-verify.py`、`provenance.json`、`tampered.py`，
  可直接跑上面那两条命令。

---

## 九、这份任务暴露并修掉的两个缺陷

1. **门禁调错解释器**：`compile`/`unit` 原先用容器内的 `sys.executable`（`python3.12`），
   KVM 节点白名单只有 `python3`，两个门禁永远 422。已改为显式 `python3`。
2. **回执判定过窄**：网关与 API 各写了一份判定，都只认 `exit` 字段，
   Agent 写 `status: "passed"` 就被判为失败，重试到超时后自我阻塞。
   现统一为 `gate_exit()`：兼容 `exit` / `exit_code` / 状态字符串，并给出准确报错。
