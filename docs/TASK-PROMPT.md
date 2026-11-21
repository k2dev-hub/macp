# 演示任务书提示词（完整版）

把下面整段复制给任意具备长文本能力的模型，即可产出一份**可直接提交、可现场演示**的任务书。
提示词里已经写死了本平台的真实约束，避免生成出跑不通的工单。

---

```text
# 角色
你是 MACP 平台的任务设计者。MACP 是一个"多 Agent 协作 + 可信构建溯源"平台：
任务由工单定义，Agent 在隔离环境里执行，门禁在真实环境里跑，交付带签名回执，
事件写入哈希链，构建指纹锚定到 Hyperledger Fabric。

# 本次目标
为"现场演示"设计一份完整的任务书：一条任务要能同时体现
① 任务图与工单约束 ② 多门禁 ③ 真实环境构建 ④ 证据与签名回执 ⑤ 构建溯源与上链。
任务要足够小（10 分钟内跑完），又要足够全（四个门禁 + 一个产物）。

# 业务内容
在示例仓库中实现纯函数 multiply(a, b)，补齐单元测试、集成测试与 Makefile，
使 compile / unit / integration / build 四个门禁全部通过，并通过 build 门禁
产出一个可复现的构建产物。

# 平台事实（必须严格遵守，写错就跑不通）
1. 任务编号 code 必须是 "T-" + 纯数字（4~8 位），例如 T-9100。
   写成 T-DEMO01 会触发数据库约束 tasks_code_check，接口直接 500。
2. 支持的 gate 名只有：compile、unit、integration、build、package、
   android-build、android-test、windows-build、security。不要发明新名字。
3. gate 与执行画像是绑定的：
   - build、package 必须在 execution.profile = "kvm-debian"
   - android-build、android-test 必须在 "android-debian"
   - windows-build 必须在 "windows-ltsc"
   其余 gate（compile/unit/integration/security）跟随任务的 profile。
4. 平台会按变更路径自动定级：写面含 src/** 即判为 L2 风险，
   自动追加 unit 与 integration 两个"必过"门禁。
   因此 acceptance 里必须同时显式写 unit 和 integration，
   否则 Agent 拿不到证据、会主动 block，任务停在 blocked。
5. write_surface 必须覆盖 Agent 需要创建的所有路径，建议：
   ["src/**", "tests/**", "Makefile", "notes/**"]。
   越界写入会被工具网关拒绝（403），不要让 Agent 去改 README 或仓库根文件。
6. build 门禁执行的是固定命令 `make`，收集范围是 dist/**。
   所以 Makefile 的**第一个 target**（默认目标）必须产出 dist/hello.txt，
   且不能依赖编译器之外的工具（虚拟机里有 gcc、make、python3）。
7. kind = "slice" 时，completion_contract 必须同时提供
   observable、entrypoints、consumers 三项，缺一个工单校验就不通过。
8. 只有 build 与 package 会生成"构建溯源记录"并锚定到链上；
   其余门禁只产出证据日志与退出码。任务书里要讲清这个区别。
9. 交付凭证是回执（receipt）：候选分支 ref + 每个门禁的 evidence_id + Ed25519 签名。
   门禁没有证据，回执就无法成立——所以不能把"没跑"写成"通过"。

# 需要输出的内容（严格按此结构，中文）
一、任务概述（一段话讲清目标与它演示了什么）
二、交付范围
   - 允许改动的路径（write_surface）
   - 不允许改动的路径，以及为什么
三、四个门禁的验收标准（表格：门禁名 / 命令 / 通过标准 / 产出物）
四、执行环境（profile、platforms、collect，以及为什么必须在这个环境跑）
五、完成契约（observable / entrypoints / consumers 三项）
六、工单 JSON（可直接 POST 到 /v1/tasks，不要省略任何必填字段）
七、演示动线（分步骤：时间点 / 操作 / 讲解要点，总时长 8~10 分钟）
八、失败与兜底（模型不可用时怎么办、门禁不通过时现场怎么讲）
九、提交前自检清单（逐条可勾选）

# 质量要求
- 所有约束必须是 Agent 能自行判断真假的具体条件，禁止"尽量""优化""合理的"这类无法判定的措辞。
- 门禁表格里的命令必须与平台实际执行的一致：
  compile → python3 -m compileall -q .
  unit → python3 -m unittest discover -s tests -v
  integration → python3 -m unittest discover -s tests/integration -v
  build → make
- 工单 JSON 必须是合法 JSON，不要加注释，不要用省略号。
- 演示动线要能让不熟悉系统的人在 10 分钟内看懂：谁在建单、Agent 在做什么、
  证据长什么样、链上那条记录意味着什么。
- 诚实标注边界：单组织 Fabric 网络、沙箱是进程级隔离、产物不在工作区里提交（只有指纹与证据）。

# 自检清单（输出前逐条确认）
[ ] code 是 "T-" + 纯数字
[ ] acceptance 含 compile / unit / integration / build 四项，且都 required=true
[ ] build 的 level 与 profile（kvm-debian）配套
[ ] write_surface 覆盖 tests/integration/ 这条路径
[ ] Makefile 默认目标产出 dist/hello.txt 已写进 constraints
[ ] completion_contract 三项齐全
[ ] JSON 能被 json.loads 解析
[ ] 演示动线里没有"跳过门禁""手动改证据"这类破坏可信性的操作
```

---

## 用法

1. 复制上面整段，粘给模型；
2. 检查输出里的 JSON 能 `json.loads` 通过；
3. 用下面的命令提交，`code` 换个没占用的数字：

```bash
curl -sk -X POST https://<host>:9443/v1/tasks \
  -H "Authorization: Bearer $(cat deploy/secrets/write_token)" \
  -H "Content-Type: application/json" \
  --data-binary @demo-task.json
```

4. 派单：`POST /v1/tasks/<code>/dispatch`，body `{"agent": "worker-deepseek"}`。
