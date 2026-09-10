# 模型流式展示、失败重试、错误认定与日志增强调研

## 1. 调研范围与结论

本文基于当前检出版本 `d93a5a75ba3b0bad9ba13c333bb0d556cd814c54` 的静态源码进行只读调研，覆盖以下四项：

1. 前端展示模型流式输出与可公开的决策依据。
2. 失败重试、失败条件和上下文管理。
3. 错误分类、错误码与最终状态认定。
4. 后端日志、前端诊断和运行指标增强。

当前系统已有 Run 级 SSE、SQLite 事件、`trace.json`、动作前后截图和结构化终态，能够展示测试任务的运行过程。模型调用仍是等待完整结构化结果后一次性返回；前端看不到模型调用阶段、增量输出和 `ToolDecision.reasoning`。重试分散在 Pydantic AI、工具动作、截图采集和 Hypium 回放四处，缺少统一错误分类、尝试记录和总预算。日志主要依赖 RunEvent、Uvicorn 输出和各类产物文件，尚未形成从 HTTP 请求到模型、动作和 HDC 命令的关联链。

推荐按以下顺序推进：

1. 先补模型调用阶段事件、错误模型、关联 ID 和脱敏规则。
2. 再修正动作重试，优先控制重复点击、重复输入等副作用风险。
3. 然后实现 SSE 断线续传、前端 reducer 和公开输出流。
4. 最后接入 token delta、指标和保留策略；进程重启后自动恢复暂不列入第一阶段。

“展示思考”建议定义为展示可公开的任务阶段、模型输出和简短决策依据。供应商隐藏推理链不进入 SSE、Trace 或报告。当前已有的 `ToolDecision.reasoning` 可以迁移为长度受限、经过脱敏的 `decision_summary`。

## 2. 当前实现链路

前端通过 `POST /api/runs` 创建任务，后端生成 `run_id` 并用 `asyncio.create_task` 启动 `AgentOrchestrator`（`web/src/App.tsx:98-108`、`src/harmony_test_agent/api/app.py:35-52`、`src/harmony_test_agent/api/app.py:146-149`）。随后前端同时建立 SSE 连接并每 800 ms 查询完整 Trace（`web/src/App.tsx:52-96`）。

运行状态包括 `created`、`preflight`、`planning`、`executing`、`verifying`、`graph_updating`、脚本阶段以及各类终态（`src/harmony_test_agent/models.py:27-57`）。RunEvent 包含 `event_id`、`run_id`、类型、时间、消息和自由结构 payload（`src/harmony_test_agent/models.py:349-357`）。每次发出事件时，系统会同步写入内存 Trace、SQLite、完整 Trace 记录和 `trace.json`（`src/harmony_test_agent/runtime/events.py:26-40`）。

模型有三类调用：规划、截图分析和动作决策。它们均调用 `await agent.run(...)`，等待 Pydantic AI 返回完整对象（`src/harmony_test_agent/agents/providers.py:271-349`）。编排器在调用完成后才发出 `plan_created` 或 `action_started`（`src/harmony_test_agent/agents/orchestrator.py:101-118`、`src/harmony_test_agent/agents/orchestrator.py:134-151`）。视觉分析还发生在 `screen_captured` 事件之前（`src/harmony_test_agent/agents/orchestrator.py:280-306`），因此长时间调用期间，前端无法判断当前正在截图、上传图像、等待模型还是校验输出。

`ToolDecision` 已含 `reasoning` 字段（`src/harmony_test_agent/models.py:242-251`）。它会进入 `action_started.payload.decision`、ActionResult 参数、Trace 和 HTML 报告（`src/harmony_test_agent/agents/orchestrator.py:147-151`、`src/harmony_test_agent/runtime/tools.py:41-47`、`src/harmony_test_agent/reporting.py:25-38`）。前端事件列表只读取 `message`、`type` 和时间，并且 Action 类型没有声明 params，所以页面没有展示该字段（`web/src/App.tsx:215-219`、`web/src/types.ts:40-59`）。

`AGENT_DISABLE_THINKING` 只控制是否向兼容端点发送 `thinking.type=disabled`（`.env.example:6`、`src/harmony_test_agent/agents/providers.py:254-257`），当前没有提取或展示供应商 reasoning 的实现。

## 3. 功能一：前端模型流式输出与决策依据

### 3.1 建议展示的内容

模型活动区可以分成四层信息：

- **阶段**：正在规划、正在分析截图、正在选择动作、正在校验结果、正在重试。
- **公开输出**：模型生成的计划描述、页面摘要和工具决策的增量文本。
- **决策依据**：最终结构化输出中的简短理由，限制长度并进行敏感字段脱敏。
- **诊断数据**：模型名、调用耗时、重试次数、token usage、完成原因和错误码。

已确认的展示方式是：页面默认显示阶段与决策摘要，原始公开输出放在折叠区域。折叠区域仍只接收经过筛选的公开输出，并允许通过配置关闭持久化。系统提示词、完整请求体、截图 OCR 原文、用户输入原文、API 密钥和供应商隐藏推理链不进入公共事件。

### 3.2 推荐事件协议

保留现有事件用于兼容，同时新增统一 SSE 事件名 `agent_event`。业务类型放在 envelope 的 `type` 字段，避免每新增一种事件都修改前端硬编码的监听列表。建议 envelope 如下：

```json
{
  "schema_version": 2,
  "event_id": 184,
  "run_id": "run-...",
  "type": "model.output.delta",
  "timestamp": "2026-09-10T05:20:00Z",
  "phase": "decision",
  "model_call_id": "mc-...",
  "step_id": "step-03",
  "sequence": 17,
  "visibility": "public",
  "payload": {
    "delta": "准备点击搜索框",
    "content_index": 0
  }
}
```

建议新增这些事件：

| 事件 | 关键字段 | 用途 |
| --- | --- | --- |
| `run.state.changed` | `from`、`to` | 状态变化后立即持久化 |
| `model.call.started` | `call_kind`、`model_call_id`、`model`、`step_id` | 前端立即显示模型正在工作 |
| `model.output.delta` | `sequence`、`delta` | 可公开输出的增量 |
| `model.decision_summary.delta` | `sequence`、`delta` | 经过脱敏的决策依据 |
| `model.call.validating` | `output_kind` | 表示开始校验完整结构化结果 |
| `model.call.completed` | `duration_ms`、`usage`、`finish_reason` | 保存最终调用结果摘要 |
| `model.call.retry_scheduled` | `attempt`、`reason`、`delay_ms` | 显示重试原因和等待时间 |
| `model.call.failed` | `error` | 统一错误 envelope |
| `model.call.cancelled` | `reason` | 确认模型调用已取消 |
| `stream.heartbeat` | `last_event_id` | 保持长连接并检测断线 |
| `stream.truncated` | `limit`、`dropped_bytes` | 告知前端增量被限流或截断 |

逐 token 调用当前 `RunEventEmitter.emit()` 会反复重写完整 Trace，带来明显写放大。实时 delta 应先进入每个 Run 的有界异步队列，每 50～100 ms 或每 20～50 个 token 合并一次 SSE chunk；数据库只保存合并后的公开检查点和最终完整结果。单事件、单调用和单 Run 均设置字节上限，慢客户端超过队列上限时发送 `stream.truncated`。

### 3.3 后端状态机

RunState 保留宏观流程，模型调用使用独立状态：

```text
queued
  -> connecting
  -> streaming
  -> validating
  -> completed

connecting / streaming / validating
  -> retry_wait
  -> connecting

任意活动态
  -> cancelling
  -> cancelled

任意活动态
  -> failed
```

增量内容只用于展示。规划、视觉观察和工具决策必须在 `validating -> completed` 后才能写入正式结果或进入 ToolExecutor，防止半截 JSON 触发设备动作。

Provider 层需要声明能力：

```text
supports_streaming
supports_structured_streaming
supports_decision_summary
supports_usage_in_stream
supports_cancel
```

不支持流式的 OpenAI 兼容端点仍发出 `started`、`validating`、`completed` 事件，让用户看到阶段和耗时。支持普通文本流但不支持结构化流时，页面可以显示临时文本，最终仍以通过 Pydantic 校验的完整对象为准。

### 3.4 SSE 与前端状态管理

当前服务端只接受 `after` 查询参数，前端没有传递游标，并在 `source.onerror` 中主动关闭 EventSource（`src/harmony_test_agent/api/app.py:169-187`、`web/src/App.tsx:52-71`）。增强后应同时支持 `Last-Event-ID` 和 `?after=`，发送心跳、`retry`、`Cache-Control: no-cache` 与禁用代理缓冲的响应头。

前端建议改用按 Run 管理的 reducer：

```ts
{
  runId,
  runState,
  connection: { state, lastEventId, retries },
  trace,
  eventsById,
  eventOrder,
  modelCalls: {
    [modelCallId]: {
      kind,
      state,
      stepId,
      output,
      decisionSummary,
      usage,
      error
    }
  }
}
```

增量文本以 50～100 ms 批量刷新，避免每个 token 触发整页重渲染。事件 JSON 增加运行时校验和 `try/catch`；当前代码直接 `JSON.parse` 后类型断言（`web/src/App.tsx:62-66`）。辅助技术只播报阶段完成和错误，不逐 token 播报。

### 3.5 最小交付方案

第一版不必立即改造 Provider 的 token streaming。先增加 `model.call.started/validating/completed/failed` 和 `decision_summary`，即可消除长调用期间的页面空白，并建立协议和 UI 骨架。第二版再接入流式 Provider、批量 delta、取消和 usage。

## 3.6 竞赛原题下的前端流程补充

竞赛原题要求系统结合应用截图、UI 布局信息、控件属性、页面跳转关系、日志和用户输入描述，形成“应用截图 / 布局采集 - 页面理解 - 交互路径规划 - 脚本生成 - 自动执行 - 结果分析 - 用例沉淀”的闭环。基础目标还要求至少识别三类常见 UI 控件或交互、生成带明确步骤和断言的可复用脚本、支持至少两类测试场景，并在至少一个 OpenHarmony 应用中覆盖不少于三个页面或核心流程。挑战目标进一步要求自然语言问题复现、页面状态图、压力测试、崩溃/卡死/白屏/布局异常分析和用例复用。原题来源：[面向 OpenHarmony 应用的多模态智能测试系统](https://competition.gitcode.com/competition/guochuang-2026/u-a24)。

当前 Web 已形成“输入任务 - 观察运行 - 查看页面图 - 查看并回放脚本 - 查看报告”的基础演示链路，但七个阶段的证据没有被完整地组织到前端。建议把现有四个结果 Tab 调整为以一次 Run 为中心的七阶段工作台，并增加运行历史和回归用例库入口。

| 阶段 | 当前前端能力 | 需要补充的主要内容 |
| --- | --- | --- |
| 截图 / 布局采集 | 展示最新截图、分辨率和截断后的元素列表（`web/src/App.tsx:210-227`） | 截图时间轴、原始布局树、布局 JSON、截图哈希、采集耗时、bbox 叠加、前后截图对比 |
| 页面理解 | 显示元素文本、类型和部分来源信息；页面图能显示节点截图和跳转关系 | 页面标题、摘要、元素置信度、来源、locator candidates、视觉依据、降级标记、节点关联元素和执行记录 |
| 交互路径规划 | 后端已经保存 `RunTrace.plan`（`src/harmony_test_agent/models.py:360-380`） | 前端计划视图：原子步骤、工具、目标、预期结果、断言、执行状态，以及计划路径和实际路径差异 |
| 脚本生成 | 显示 Hypium Python 和 warnings（`web/src/App.tsx:236-241`） | Python、JSON 配置、metadata 三视图；Action/Assertion 来源映射；语法检查、坐标降级和生成阶段进度 |
| 自动执行 | 可手动触发连续三次回放（`web/src/App.tsx:136-147`） | “生成后自动执行”开关；每次 attempt 的退出码、耗时、stdout/stderr、失败截图、报告路径和 SSE 实时进度 |
| 结果分析 | 展示状态和数量统计，提供 HTML 报告 | 逐步耗时、失败步骤、错误分类、断言、重试、定位降级、截图差异、稳定性统计，以及崩溃、卡死、白屏、无响应和布局异常判定 |
| 用例沉淀 | SQLite 和 Run 目录保留历史 Trace 与产物，后端已有 Run 列表接口（`src/harmony_test_agent/api/app.py:141-144`） | 历史 Run 页面；将成功 Run 保存为命名用例；标签、版本、套件、基线、审核状态、重跑、导入导出和来源追踪 |

建议前端一级导航为“新建任务、运行历史、回归用例库、页面模型、验收证据”。新建任务页选择应用、Profile、设备、模式和自然语言目标；模式应真正影响规划参数，例如探索预算、稳定性次数和问题复现前置状态。当前规划调用只传任务、Profile 和步数，没有传 `request.mode`（`src/harmony_test_agent/agents/orchestrator.py:101-117`），前端增加模式参数时需要同步修正后端语义。

Run 工作台顶部使用七阶段进度条，阶段下提供以下可审查内容：

1. **采集**：截图时间轴、当前截图、布局树、布局 JSON、分辨率、SHA-256 和 bbox 图层。
2. **理解**：页面标题与摘要、元素表、来源、置信度、交互属性、定位候选和降级原因。
3. **规划**：计划步骤、工具、目标、预期页面、检查点、当前执行状态、计划路径和实际页面图。
4. **生成**：Python、JSON 和 metadata；显示每段脚本对应的 Action/Assertion，以及语法检查和生成警告。
5. **执行**：每个回放 attempt 独立展示准备、执行、退出码、标准输出、错误输出、失败截图和报告。
6. **分析**：通过率、逐步耗时、断言、错误分类、重试、页面差异、异常检测和多次运行稳定性。
7. **沉淀**：保存为命名用例，设置版本、标签、测试套件、基线和审核状态，记录来源 Run、Profile hash 和脚本 hash。

页面图作为“理解”和“规划”的共享视图。当前节点详情已有截图、页面路径、元素数量和入出边（`web/src/components/PageGraphView.tsx:395-435`），下一步补关联 Snapshot、完整元素列表、进入/离开 Action、断言和失败证据。报告页保留为分析结果的导出视图，统一提供 HTML、JSON、Hypium 报告和验收证据索引。

前端补齐顺序建议如下：

1. 先展示后端已有的 `plan`、`actions`、`assertions`、`replays` 和 Run 列表，形成七阶段导航及证据骨架。
2. 增加模型阶段、默认决策摘要、折叠原始公开输出、SSE 连接状态和结构化错误。
3. 增加截图时间轴、布局树、bbox 叠加、节点元素详情和计划/实际路径对照。
4. 增加回放 attempt 详情、实时执行事件和结果分析面板。
5. 建立可版本化的回归用例库，完成“用例沉淀”闭环。

第一、二步主要消费现有后端数据，投入相对可控，并能明显提高竞赛演示时的可解释性。第三至第五步需要扩展 API、存储模型和运行事件。

## 4. 功能二：失败重试与上下文管理

### 4.1 当前重试分布

| 层 | 当前机制 | 主要问题 |
| --- | --- | --- |
| Provider | 三类 Agent 都写死 `retries=2`（`src/harmony_test_agent/agents/providers.py:271-349`） | 未按 408、429、5xx、鉴权、额度、上下文过长和校验失败分类；无法通过配置调整 |
| 工具动作 | `AGENT_RETRY_LIMIT` 默认 2，总尝试 3 次，线性等待（`src/harmony_test_agent/config.py:33-36`、`src/harmony_test_agent/agents/orchestrator.py:317-338`） | 复用旧 snapshot 和旧 decision；动作副作用未知时仍可能直接重复 |
| 截图 | capture/recv 固定最多 3 次（`src/harmony_test_agent/devices/harmony.py:144-177`） | 不进入统一预算，没有事件和取消检查 |
| Hypium | 固定执行 1～3 次并要求全部通过（`src/harmony_test_agent/runner/hypium.py:33-99`、`src/harmony_test_agent/api/app.py:222-235`） | 这是稳定性采样，不能作为执行恢复重试 |

工具动作通过 `asyncio.to_thread()` 执行，再由 `asyncio.wait_for()` 增加外层超时（`src/harmony_test_agent/agents/orchestrator.py:317-324`）。取消 await 无法保证已经运行的线程立刻结束。点击、输入、返回或滑动已经到达设备后，编排器仍可能收到超时并发起下一次尝试，从而产生重复副作用。

当前每次尝试复用同一 snapshot 和 decision。失败后没有重新截图、重新定位、重新决策，也没有把失败原因反馈给模型。元素缺失直接作为不可重试错误（`src/harmony_test_agent/agents/orchestrator.py:332-336`），无法覆盖页面加载、弹层变化和 UI hierarchy 暂时滞后等常见瞬时情况。

### 4.2 重试必须先判断副作用

建议给操作定义 effect 属性：

| effect | 操作示例 | 处理方式 |
| --- | --- | --- |
| `none` | 模型分析、health check、读取 Trace、截图读取失败 | 满足错误分类后可直接重试 |
| `reconcilable` | `open_app`、部分等待与页面刷新 | 重试前检查当前状态 |
| `unknown_on_timeout` | 点击、输入、返回、滑动 | 超时后先重新截图和核对后置条件 |
| `confirmed` | 已通过后置条件或页面签名确认生效 | 提交 step，不再重放 |

副作用未知时进入 reconciliation：

1. 保存原 `operation_id`、before snapshot、参数摘要和预期后置条件。
2. 重新采集 screenshot 与 hierarchy。
3. 检查目标页面、元素、输入文本摘要或页面图签名。
4. 已生效则提交动作；确认未生效才允许重试；无法判断时进入 `needs_attention`。

输入文本默认不自动重复。除非能证明输入框仍为空，并且页面、焦点和元素版本均与准备动作时一致。

### 4.3 推荐重试状态机

```text
step_ready
  -> deciding
  -> action_prepared
  -> action_in_flight
  -> observing
  -> validating
       -> step_committed -> step_ready
       -> retry_wait -> deciding
       -> refresh_context -> deciding
       -> reconciling
            -> effect_confirmed -> step_committed
            -> effect_absent -> retry_wait
            -> effect_unknown -> needs_attention
       -> failed
```

`action_prepared` 必须在设备调用前持久化，至少包含：

```text
operation_id
step_id
attempt
context_version
before_snapshot_id
page_signature
normalized parameters
expected postcondition
effect policy
```

每次尝试单独记录，最终 ActionResult 不能覆盖尝试历史。退避建议使用指数增长、随机抖动和 Provider 的 `Retry-After`，并设置 operation、step、run 三层预算。

建议初始预算：

| 场景 | 尝试 | 退避 | 备注 |
| --- | ---: | --- | --- |
| Provider 408/429/502/503/504 | 最多 3 次 | 0.5s、1s、2s + jitter，优先 `Retry-After` | 受 Run 级模型调用总预算约束 |
| Provider 输出校验失败 | 最多 2 次修复 | 立即或 0.2s | 提供校验摘要，不回传完整敏感输出 |
| screenshot/只读 HDC | 最多 3 次 | 0.3s、0.8s | 检查设备连接和取消状态 |
| 元素暂未出现 | 2 次刷新上下文 | 0.5s、1s | 每次重新截图和定位 |
| 点击/输入/返回/滑动超时 | 0 次直接重放 | 先 reconciliation | 只有确认未生效才重试 |
| 断言失败 | 2 次 eventual-consistency 观察 | 0.5s、1s | 重新采集后仍失败才定案 |
| 401/403/配置缺失/安全拒绝 | 0 次 | 无 | 立即失败 |

### 4.4 上下文版本与恢复

当前模型调用采用无会话方式：规划获得任务与应用信息，决策获得当前 step、最多 120 个元素和当前截图（`src/harmony_test_agent/agents/providers.py:271-349`）。这控制了上下文大小，但重试时看不到先前失败、页面变化和尝试次数。

建议引入 `ExecutionContext`：

```text
context_version
run_id
step_index
step_id
attempt
current_snapshot_id
page_signature
previous_action_summary
last_error_code
retry_reason
remaining_budget
```

每次重新截图后递增 `context_version`。Provider 决策必须声明它基于哪个版本；执行前若当前版本已经变化，则丢弃旧决策并重新生成。

恢复边界设在 `step_committed`。进程重启扫描非终态 Run 时进入 `recovering`，校验设备、执行租约、计划版本和最后 checkpoint。未确认的 `action_in_flight` 先 reconciliation。旧 Trace 缺少 operation ledger 时标记为不可自动恢复，保留手动重新运行入口。

Run 创建接口还需要幂等键。当前每次 `POST /api/runs` 都创建新 run_id（`src/harmony_test_agent/api/app.py:35-52`）。客户端超时后重发可能同时操纵同一设备。建议接受 `Idempotency-Key`，在有限时间内对同一调用返回原 run_id，并对设备设置单运行租约。

### 4.5 停止语义

当前停止请求只在每个计划步骤开始时检查（`src/harmony_test_agent/agents/orchestrator.py:120-126`、`src/harmony_test_agent/agents/orchestrator.py:366-370`）。模型调用、动作、settle、截图、生成和回放期间的停止不会立即生效。停止 API 对活动任务只设置内存标记，却立即向前端返回 `stopped_by_user`（`src/harmony_test_agent/api/app.py:54-59`、`src/harmony_test_agent/api/app.py:162-167`）。

建议引入 `cancelling`：

```text
任意非终态 -> cancelling
cancelling -> cancelled
cancelling -> reconciling -> cancelled
```

API 先返回 `cancelling`。Provider、重试等待、settle、截图和步骤边界均检查 cancellation token。存在未知副作用时完成一次 reconciliation，再写入最终 `cancelled`。服务关闭导致的中断使用 `interrupted`，便于后续恢复和用户取消区分。

### 4.6 服务重启后自动继续的含义与适用场景

这里的“服务重启”指运行 Agent 的 FastAPI/Python 进程在一个 Run 尚未结束时退出并重新启动，例如开发时修改代码触发 reload、API 进程崩溃、电脑重启、工具升级后重启服务，或者统一启动器发现 API 异常并重新拉起。当前活动任务只保存在 `RunManager.tasks` 和 `RunManager.orchestrators` 的进程内字典中（`src/harmony_test_agent/api/app.py:28-33`）。进程退出后，SQLite 和 Run 产物仍存在，但对应的 asyncio task、设备连接和内存停止标记都会消失。重新启动服务只能查看旧 Trace，不能从中断位置继续。

“自动继续”意味着新进程启动后扫描非终态 Run，重新取得同一设备的执行租约，读取最近一次已经提交的 step checkpoint，检查当前设备页面是否仍与 checkpoint 匹配，然后从下一步继续。如果退出时某个点击或输入已经发出、结果尚未确认，恢复程序还必须先进入 reconciliation，不能直接重放该动作。

典型场景如下：

```text
Run 正在第 6 步输入文本
  -> API 进程崩溃或电脑重启
  -> SQLite 中最后确认完成的是第 5 步
  -> 新进程启动，发现该 Run 仍是 executing
  -> 重新连接设备并采集当前页面
  -> 判断第 6 步是否已经生效
  -> 已生效则从第 7 步继续
  -> 未生效且安全则重试第 6 步
  -> 无法判断则进入 needs_attention
```

这个能力主要服务长时间探索、稳定性压测、无人值守执行和未来平台化部署。对于当前单机竞赛 MVP，单次 Run 通常较短，设备页面在服务重启后还可能被用户操作、应用重启或系统弹窗改变，因此第一阶段自动继续的收益低于实现复杂度和误操作风险。

本方案建议第一阶段不做自动继续。服务启动时只把遗留非终态 Run 标记为 `interrupted`，保留完整证据，并提供“重新运行”和未来的“检查后恢复”入口。数据模型从第一阶段预留 `step_index`、`operation_id`、`context_version`、checkpoint 和 effect 字段。等稳定性压测、长时间探索或无人值守成为明确验收要求后，再实现自动恢复。

## 5. 功能三：错误认定

### 5.1 当前问题

现有终态按失败层划分：`failed_device`、`failed_model`、`failed_element`、`failed_action`、`failed_assertion` 和 `failed_script`（`src/harmony_test_agent/models.py:27-57`）。同一根因可能映射成不同终态：设备在预检时断开会得到 `failed_device`，设备命令执行中断开可能只产生普通非零 CommandResult，随后成为 `failed_action`（`src/harmony_test_agent/devices/harmony.py:43-90`、`src/harmony_test_agent/runtime/tools.py:89-92`）。

真实 Provider 的视觉异常全部变成 `failed_model`，其中也可能包含图片读取、本地文件和序列化错误（`src/harmony_test_agent/agents/orchestrator.py:280-294`）。安全策略拒绝最终也会落到 `failed_action`。Hypium 的 JSON 缺失、损坏、断言失败、设备断开和超时都只表现为 `passed=false`（`src/harmony_test_agent/runner/hypium.py:83-95`）。

此外，动作返回成功后才比较页面是否连续无变化。达到阈值时 Run 会失败，但最后一条 ActionResult 仍为成功（`src/harmony_test_agent/agents/orchestrator.py:190-221`），前端可能同时看到“动作成功”和“运行因动作失败结束”。

### 5.2 统一错误 envelope

建议增加稳定的错误结构，终态继续用于用户界面，错误 envelope 用于诊断和重试决策：

```json
{
  "code": "DEVICE_COMMAND_TIMEOUT",
  "layer": "device",
  "category": "transient",
  "retryable": false,
  "effect": "unknown",
  "operation": "input_text",
  "run_id": "run-...",
  "step_id": "step-03",
  "attempt": 1,
  "context_version": 7,
  "before_snapshot_id": "snap-...",
  "provider_request_id": null,
  "http_status": null,
  "command_returncode": null,
  "message": "设备命令超时，动作效果待确认",
  "cause_type": "TimeoutError",
  "retry_after_ms": null
}
```

建议 category：

| category | 含义 | 示例 |
| --- | --- | --- |
| `transient` | 外部依赖短暂不可用 | 408、429、部分 5xx、临时设备连接失败 |
| `permanent` | 当前输入或配置下重试不会改善 | 401、403、模型未配置、非法参数、安全拒绝 |
| `invalid_result` | 调用完成但结果不满足协议或语义 | 结构化输出校验失败、计划无 finish、结果文件损坏 |
| `assertion` | 被测业务结果不符合预期 | 稳定观察后断言仍失败 |
| `cancelled` | 用户明确停止且后端已完成收敛 | cancellation token 生效 |
| `interrupted` | 服务关闭、进程退出或租约失效 | 可进入恢复流程 |

错误码采用稳定大写枚举。建议第一批覆盖：

```text
MODEL_TIMEOUT
MODEL_RATE_LIMITED
MODEL_AUTH_FAILED
MODEL_OUTPUT_INVALID
MODEL_CONTEXT_TOO_LARGE
MODEL_CAPABILITY_UNSUPPORTED
DEVICE_DISCONNECTED
DEVICE_COMMAND_TIMEOUT
DEVICE_COMMAND_FAILED
SCREEN_CAPTURE_FAILED
ELEMENT_NOT_FOUND
STALE_EXECUTION_CONTEXT
ACTION_EFFECT_UNKNOWN
ACTION_POSTCONDITION_FAILED
ASSERTION_FAILED
SAFETY_POLICY_REJECTED
SCRIPT_PROCESS_FAILED
SCRIPT_RESULT_MISSING
SCRIPT_RESULT_INVALID
RUN_CANCELLED
RUN_INTERRUPTED
STORAGE_WRITE_FAILED
SSE_CONNECTION_LOST
FRONTEND_PROTOCOL_ERROR
```

### 5.3 错误判定顺序

一次操作结束后按以下顺序认定：

1. 检查用户取消或系统中止。
2. 检查安全策略和参数协议。
3. 检查外部调用是否确实完成。
4. 检查返回结果能否解析和通过 schema 校验。
5. 检查动作副作用状态。
6. 检查后置条件或断言。
7. 依据 category、effect 和预算确定重试、刷新上下文、reconciliation 或终止。

HTTP 状态码、异常类型或命令返回码只能作为输入。最终判定还要结合当前操作、是否产生副作用、上下文版本和后置条件。

### 5.4 断言失败与系统错误

断言失败是测试结论，日志级别使用 INFO 或 WARNING；执行框架异常、持久化失败和无法确认的设备副作用使用 ERROR。这样服务错误率不会被预期的负向测试结果污染。

## 6. 功能四：日志增强

### 6.1 当前能力与缺口

`run_id + event_id + step_id` 已能回溯一次 Run 的业务时间线。Run Trace 保存任务、设备、模型、计划、截图、动作、断言、图、回放和最终错误（`src/harmony_test_agent/models.py:360-381`）。CommandResult 保存完整命令、参数、stdout、stderr、返回码、超时和耗时（`src/harmony_test_agent/models.py:254-268`）。

配置中没有日志级别、格式、目录、轮转大小和保留天数（`src/harmony_test_agent/config.py:18-37`、`.env.example:1-19`）。FastAPI 没有 request ID 中间件；Uvicorn 没有统一 log config。开发启动器把 API 与 Vite 输出合并后打印到终端（`src/harmony_test_agent/devserver.py:146-165`、`src/harmony_test_agent/devserver.py:227-240`）。前端只有页面内 `setError()`，没有 Error Boundary、`window.onerror`、`unhandledrejection` 或上报接口（`web/src/App.tsx:40-47`、`web/src/App.tsx:74-111`）。

当前每个事件都会重写完整 Trace，运行越长，SQLite 与文件写放大越明显。设备 `hilog`、Hypium stdout/stderr、截图、布局和 Run 数据没有自动 TTL 或容量上限。清理依赖人工脚本。

### 6.2 统一关联链

建议建立以下链路：

```text
request_id
  -> run_id
      -> model_call_id
      -> step_id
          -> action_id
              -> attempt
              -> command_id
```

基础日志字段：

```json
{
  "schema_version": 1,
  "timestamp": "2026-09-10T05:20:00Z",
  "level": "INFO",
  "event": "action.finished",
  "service": "harmony-test-agent",
  "component": "orchestrator",
  "request_id": "req-...",
  "run_id": "run-...",
  "step_id": "step-03",
  "action_id": "act-...",
  "attempt": 2,
  "model_call_id": null,
  "command_id": "cmd-...",
  "operation": "input_text",
  "outcome": "success",
  "duration_ms": 842,
  "error": null,
  "attributes": {}
}
```

HTTP 中间件生成或接受 `X-Request-ID`，并在响应头回传。创建 Run 时把 request ID 写入 Run 上下文。模型、动作和 HDC 命令分别生成调用 ID。设备 ID 在应用日志中使用稳定哈希；原值只留在访问受控的 Run 证据中。

### 6.3 日志与事件分工

- **RunEvent**：用户可见的业务时间线，内容经过筛选和脱敏，通过 SSE 展示。
- **应用 JSON 日志**：开发和运维诊断，记录 HTTP、模型、编排、设备、存储和异常。
- **Run 证据**：截图、布局、完整命令结果、回放 stdout/stderr 和报告，按访问权限与保留策略管理。
- **指标**：聚合成功率、耗时、重试与容量，不包含高基数 ID 或任务内容。

推荐事件名：

```text
http.request.started / http.request.finished
run.started / run.state_changed / run.finished
model.call.started / model.call.finished
action.prepared / action.retry / action.finished
device.command.started / device.command.finished
artifact.written
sse.connected / sse.disconnected
frontend.error
retention.cleanup
```

### 6.4 脱敏边界

默认按受限数据处理以下字段：

- 任务原文、输入文本、目标文本和决策依据。
- 模型 prompt、完整响应和 UI hierarchy 内容。
- 截图、设备 ID、HDC 路径和本机绝对路径。
- CommandResult 的 command、args、stdout 和 stderr。
- 设备 hilog 与 Hypium 日志。

应用日志只保留长度、哈希、类型、截断错误摘要和 artifact 引用。`input_text` 参数需在进入 ActionResult 和 command artifact 前按字段语义脱敏，避免密码、手机号和 token 重复落盘。前端错误上报只发送版本、页面、run/request ID、错误类型和截断堆栈。

### 6.5 日志级别

| 级别 | 事件 |
| --- | --- |
| DEBUG | 定位器候选数、截图哈希、输入字节数、脱敏 HDC 参数、退避和 SSE 游标 |
| INFO | 服务生命周期、HTTP 完成、Run 状态、模型/动作/命令成功、产物创建 |
| WARNING | 重试、限流、SSE 中断、页面连续无变化、降级定位器、容量接近上限 |
| ERROR | Run 框架失败、模型/设备/脚本最终失败、前端未处理异常、持久化失败 |
| CRITICAL | 数据库无法打开、运行目录不可写、日志系统失效 |

### 6.6 首批指标

```text
http_requests_total{method,route,status_class}
http_request_duration_seconds{method,route}
runs_total{mode,outcome}
runs_active
run_duration_seconds{mode,outcome}
model_calls_total{operation,provider,model,outcome}
model_call_duration_seconds{operation,provider,model}
model_tokens_total{operation,provider,model,direction}
model_retries_total{operation,reason}
actions_total{tool,outcome}
action_duration_seconds{tool,outcome}
action_retries_total{tool,reason}
device_commands_total{operation,outcome}
device_command_duration_seconds{operation,outcome}
sse_connections_active
sse_disconnects_total{reason}
frontend_errors_total{kind,release}
artifact_write_failures_total{kind}
runtime_storage_bytes
retention_deleted_runs_total
```

指标标签只使用 route、operation、provider、model、tool、outcome 和 error_type 等低基数字段。request_id、run_id、step_id、设备 ID 和任务文本不进入标签。

### 6.7 存储与保留

单机 MVP 可以先使用标准库 logging 的 JSON formatter 和 RotatingFileHandler，避免首版引入完整外部可观测栈。建议默认：

- `app.log`：10 MB × 5。
- `error.log`：10 MB × 10。
- `access.log`：10 MB × 5。
- Run 产物：按天数和总容量双阈值清理。
- hilog、stdout、stderr：单文件字节上限，保留截断标记。
- 事件：增量落库；Trace 快照按关键状态或节流周期写入。

外部检索和告警需求明确后，再把相同 JSON 字段映射到 OpenTelemetry、Loki 或其他平台。

## 7. 推荐实施分期

### 阶段 A：可见性与错误基础

目标：不改变设备执行语义，先让页面知道系统在做什么，并让错误可分类。

- 定义 `ErrorEnvelope`、错误码、事件 schema version 和数据可见性。
- 增加 `request_id`、`model_call_id`、`action_id`、`command_id`。
- 增加模型 started/validating/completed/failed 事件和最终 decision summary。
- 增加 JSON 应用日志、访问日志、错误日志和统一脱敏。
- 前端增加模型活动区、SSE 连接状态和事件运行时校验。

验收：模型长调用期间 1 秒内出现阶段信息；任一失败可用 run_id 关联到模型调用或设备命令；应用日志不出现 API key 与 input_text 原文。

### 阶段 B：安全重试

目标：避免副作用动作被机械重复，并使短暂页面问题能够恢复。

- 增加 operation attempt ledger 与 `action_prepared/in_flight`。
- 增加 effect policy、context version 和 reconciliation。
- 元素缺失时刷新截图、重新定位和重新决策。
- Provider 采用统一错误分类、指数退避、抖动和总预算。
- Run 创建加入 Idempotency-Key 和设备租约。
- 停止流程增加 cancelling/cancelled/interrupted。

验收：输入和点击超时不会直接重放；同一幂等键不产生两个 Run；旧 context 的决策不会执行；停止后的最终状态与后端实际状态一致。

### 阶段 C：流式协议与断线恢复

目标：展示可公开模型输出，并确保断线后恢复。

- Provider 能力协商和 token/文本流适配。
- 有界流队列、批量 delta、字节限额和慢客户端截断。
- SSE 支持 Last-Event-ID、after、心跳、retry 和重连。
- 前端 reducer、按 sequence 合并、50～100 ms 批量渲染。
- 流式结构化输出完成校验前禁止执行。

验收：网络断开后从最后 event ID 恢复；无重复 delta；慢客户端不拖垮 Run；不支持 streaming 的 Provider 仍能完整显示阶段。

### 阶段 D：恢复与可观测性

目标：支持服务重启恢复并形成运行质量指标。

- 持久化 step checkpoint、执行租约和恢复元数据。
- 启动扫描非终态 Run，进入 recovering/reconciling。
- 增加 resume API 和 needs_attention 操作入口。
- 增加 `/metrics`、容量监测、保留策略和清理事件。
- 为日志、事件和 Trace 减少重复写入。

验收：进程在 step 边界退出后可继续；未知副作用不会自动重放；指标能区分模型、动作、设备和断言失败。

## 8. 必要验证

建议实现时补充以下测试，集中覆盖行为风险，不测试日志文案本身：

1. Provider 429 遵循 Retry-After，并受总预算限制。
2. 点击、输入超时后先 reconciliation，不直接执行第二次。
3. 元素缺失后重新截图、递增 context version 并重新决策。
4. 旧 context 的模型决策被拒绝。
5. Idempotency-Key 重复请求返回同一个 run_id。
6. 停止发生在模型、动作、截图和最后一步时均得到一致终态。
7. 服务退出后 `interrupted` Run 能从 committed step 恢复。
8. SSE 断线按 Last-Event-ID 恢复，不丢失、不重复事件。
9. 流式半截 JSON 不进入 ToolExecutor。
10. delta 合并与队列上限不会导致无限内存增长。
11. input_text、API key、Authorization、绝对路径不会出现在应用日志和前端错误上报。
12. 断言失败计入测试结果，不计入服务框架 ERROR。
13. 应用日志轮转、Run TTL 和容量阈值生效。
14. 并发 generate/execute 被状态门禁和 Run 锁拒绝。

当前失败测试仅覆盖视觉超时映射为 `failed_model` 和设备预检失败映射为 `failed_device`（`tests/integration/test_failure_states.py:73-119`）。SSE API 测试只检查终态事件可读取（`tests/api/test_api_contract.py:105-108`）。上述场景需要在实施时补齐。

## 9. 需要讨论并确定的方案

以下决策会直接影响接口和数据模型。建议先确定前四项，再开始实施。

1. **模型展示范围**：默认只展示阶段与 decision summary，还是同时展示原始公开输出增量？推荐默认展示阶段与摘要，原始输出放在可展开区域。
2. **流式持久化**：只保存合并检查点与最终结果，还是保存全部 delta？推荐保存检查点与最终结果，减少敏感数据和写放大。
3. **副作用未知的处理**：进入 `needs_attention` 等待人工确认，还是由页面后置条件自动裁决？推荐先自动 reconciliation，无法确认时停止并等待人工处理。
4. **恢复目标**：第一版只做单次运行内安全重试，还是同步支持进程重启恢复？推荐先完成运行内重试，数据模型从第一版预留恢复字段。
5. **断言重试**：允许几次 eventual-consistency 观察？推荐默认 2 次，仅重新采集和判断，不重复前一个业务动作。
6. **Run 并发**：同一设备是否严格只允许一个活动 Run？推荐严格单租约；不同设备可以并行。
7. **日志平台**：第一版使用本地 JSON 轮转文件和 `/metrics`，还是直接接 Loki/OTel？推荐先落地本地结构化日志，字段设计保持可映射。
8. **敏感证据保留**：任务原文、输入文本、截图和完整模型响应保留多久？建议分别设置保留级别，input_text 默认脱敏，截图和完整响应按 Run 证据策略限期保存。
9. **兼容策略**：是否需要保持旧前端与旧 Trace 可读？推荐事件协议增加 schema version，并保留旧事件至少一个版本周期。
10. **错误终态**：是否新增 `failed_invalid_result`、`needs_attention`、`cancelled` 和 `interrupted`？推荐新增；旧 `stopped_by_user` 在兼容层映射到 `cancelled`。

## 9.1 本轮已确认和待确认的决策

| 项目 | 当前决定 | 对方案的影响 |
| --- | --- | --- |
| 模型展示 | 默认显示阶段与决策摘要；原始公开输出折叠展示 | 前端模型活动区默认简洁，折叠内容仍需脱敏和限流 |
| 动作效果无法确认 | 尚待最终确认；建议允许进入 `needs_attention` | 避免重复点击、重复输入和多次返回；前端需提供人工处置入口 |
| 同一设备并发 | 同一设备强制单 Run，不同设备可并行 | 需要设备租约、Run 创建冲突提示和释放机制 |
| 服务重启恢复 | 第一阶段不自动继续；遗留 Run 标记为 `interrupted`，预留恢复字段 | 降低第一版复杂度，避免设备状态漂移时误恢复 |
| 前端组织 | 按竞赛原题七阶段构建 Run 工作台，并增加运行历史与回归用例库 | UI 从四个结果 Tab 扩展为完整证据链和用例资产入口 |

`needs_attention` 的具体含义是：设备命令已经发出，但系统无法确认动作是否生效，此时 Run 停止自动推进，并显示动作、before/after 截图、命令结果、错误原因和三种处置：确认已生效并继续、确认未生效并安全重试、终止 Run。若不允许该状态，系统只能在“可能重复执行”和“直接失败”之间选择。对于输入、返回、切换开关和具有提交语义的点击，建议允许 `needs_attention`。

## 10. 建议的讨论起点

建议以“阶段 A + 阶段 B”为首个实施范围：先做可见性、统一错误和安全重试，暂不承诺所有兼容模型都能输出 token delta。这个范围可以直接解决用户看不到模型活动、错误只剩字符串、动作可能重复执行、日志无法串联四个当前问题，也为后续完整流式协议和进程恢复建立稳定的数据模型。

下一轮讨论只需继续确认以下实现边界：

1. 自动 reconciliation 仍无结论时，是否正式接受 Run 停在 `needs_attention`？
2. `needs_attention` 是否同时提供“确认已生效并继续”“确认未生效并安全重试”“终止 Run”三个操作？
3. 第一阶段的运行历史和回归用例库只做只读查看，还是支持命名、标签、版本和一键重跑？
4. 折叠区域中的原始公开输出是否落盘；若落盘，保留期限如何设置？

确认这些边界后，可以把阶段 A 与阶段 B 细化为文件级改动清单、API schema、迁移策略和验收用例。
