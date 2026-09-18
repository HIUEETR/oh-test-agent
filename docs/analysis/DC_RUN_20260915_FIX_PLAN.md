# DC 运行 20260915T091325Z 修复计划

> ⚠️ 历史设计文档（2026-09-17 重构前）。当前架构见 docs/ARCHITECTURE.md §16。本文保留仅供追溯。

## 1. 文档边界

本文档是给后续实现 Agent 的修复计划，不直接修改产品行为。分析基于：

- 运行产物：`D:\Work\Code\Harmony\mvp\artifacts\runs\dc-20260915T091325Z-b4a72348`
- 运行快照：`dc_session.json`
- 运行时分支：`dc-mode-issues-fix`，提交 `8416a96`，工作树为
  `D:\Work\Code\Harmony\mvp`
- DC 源码：`D:\Work\Code\Harmony\mvp\src\harmony_test_agent\dc\`
- DC 前端：`D:\Work\Code\Harmony\mvp\web\src\{api,stores,features\dc\}`
- 用户附图：操作日志面板截图。附图只作为 UI 现象证据，不包含需要照搬执行的操作指令。

本文不把供应商隐藏推理链当作产品输出。前端应展示阶段、公开叙述、脱敏后的决策摘要、工具状态和错误证据；原始隐藏 reasoning 不应进入 SSE、持久化快照或后续模型上下文。

## 2. 运行证据

运行 `dc-20260915T091325Z-b4a72348` 的关键事实：

| 项目 | 证据 |
| --- | --- |
| 会话创建 | `2026-09-15 09:13:25.724753Z` |
| 第一轮 | `turn-0429c276`，`09:14:26.256454Z` 到 `09:22:47.121486Z` |
| 第一轮结果 | `cancelled`，13 个模型步骤，23 次工具调用，`agent_summary` 为空 |
| 第一轮工具 | 23 次均有 `turn_id=turn-0429c276`，但该轮 `invocation_ids` 为空 |
| 最长无新工具记录区间 | `09:18:36.751Z` 到 `09:23:26.192Z`，约 289.44 秒 |
| 第二轮 | `turn-56a24dcc`，用户消息为“你刚刚在执行什么卡了这么久，现在继续” |
| 第二轮工具 | `screenshot` 14.356 秒，`foreground_app` 10.602 秒 |
| 第二轮结果 | 3 个模型步骤，输出以“需要帮助”开头，声称没有上一轮具体任务上下文 |
| 快照最终状态 | session `idle`，总计 25 次工具调用，最新截图路径存在 |

第一轮的截图和 UI 层级调用本身也有明显耗时：

- `screenshot` 耗时约 8.713 到 16.051 秒；
- `dump_ui_hierarchy` 耗时约 5.726 到 10.463 秒；
- 最后一条第一轮工具 `screenshot` 在 `09:18:23.182Z` 开始，`09:18:36.751Z` 结束；
- 随后约 4 分 49 秒没有新的工具事件或用户可见进度。

这说明问题 2 不能只归因于某一次 HDC 截图慢。截图慢需要优化和分阶段计时，但“长时间完全没有输出”的直接缺口是 DC Provider 没有使用 `AGENT_MODEL_TIMEOUT`，也没有模型调用开始、心跳、超时和取消事件。

## 3. 根因确认

### 3.1 操作日志为空或长期显示 `0 步`

前端 `web/src/features/dc/DcOperationLog.tsx:13-35` 只从
`state.session?.invocations` 读取数据。它没有读取 `messages`，也没有读取
SSE 事件中的工具调用。

`web/src/stores/dc-console.ts:302-381` 收到
`tool_call_started` 和 `tool_call_finished` 时只更新聊天消息，未更新
`state.session.invocations`。`refreshSession()` 只在会话创建、选择会话、SSE
重连和脚本生成等路径触发；正常的 `turn_finished` 不会刷新会话投影。

因此存在两个时间窗口：

1. 工具刚开始时，后端还没有把 `DcToolInvocation` 追加到
   `DcActionRecorder.invocations`，日志面板必然仍显示 0。
2. 工具完成后，SSE 已经更新聊天工具卡，但 `session` 仍是创建时或上一次
   刷新时的快照，日志面板仍显示 0。

附图中的“操作日志 / 0 步 / 尚无操作记录”与这个数据流完全一致。它不能证明
后端没有执行操作，因为运行快照中实际存在 25 条 `invocations`。

后端也存在数据一致性缺口：

- `src/harmony_test_agent/dc/tools.py:133-136` 的注释和实现都表明，
  工具记录在工具完成后才追加；
- `src/harmony_test_agent/dc/session.py:360` 只在正常完成路径设置
  `turn.invocation_ids`；
- `src/harmony_test_agent/dc/session.py:386-390` 的取消分支没有设置该字段，
  所以被取消的第一轮虽然有 23 条全局 invocation，轮次自身仍显示 0 条关联操作；
- `src/harmony_test_agent/dc/session.py:401` 只在轮次 finally 落盘，长时间运行
  中途崩溃时不会保存当前工具和当前上下文。

### 3.2 工具或模型长时间运行时没有输出

当前 DC Provider 在 `src/harmony_test_agent/dc/provider.py:71-134` 使用
`agent.iter()`，但迭代过程没有包在 `settings.agent_model_timeout` 约束中。
配置项 `AGENT_MODEL_TIMEOUT=90` 在 `src/harmony_test_agent/config.py:33-36`
存在，但现有 DC 路径没有消费它。`agent.iter()` 在等待模型响应期间可能长时间
不产生 `THINKING` 或 `AGENT_TEXT` 事件。

工具层虽然在 `src/harmony_test_agent/dc/tools.py:155-159` 使用
`asyncio.wait_for(asyncio.to_thread(device_fn), timeout=...)`，但这只能取消
等待 Future，不能保证已经启动的线程或其中的 `subprocess.run` 被终止。取消或
超时后，底层 HDC 进程可能仍在执行。当前 UI 没有显示：

- 正在等待模型还是正在等待 HDC；
- 当前调用的工具、调用 ID、开始时间和已耗时；
- 当前阶段的最后心跳时间；
- 超时是否已经发生；
- 设备副作用是否已经确认；
- 是否可以安全继续、需要重新观测，还是必须人工处理。

运行中的 289 秒静默区间因此既不能被用户理解，也不能被系统自动分类。

### 3.3 新消息后丢失上一轮任务上下文

第一轮被取消时，`src/harmony_test_agent/dc/session.py:354` 的
`self.history = response.history` 尚未执行。随后取消分支只设置状态并重新抛出
`CancelledError`，最后在 `:398-401` 把空的 `self.history` 落盘。

第二轮开始时，`src/harmony_test_agent/dc/session.py:309-310` 只从
`self.history` 生成 `pruned_history`。因为第一轮未成功返回，第二轮没有收到
第一轮的模型消息，也没有收到第一轮的用户目标、已完成操作列表、最后一帧或
未确认动作信息。`DC_TURN_TEMPLATE` 只包含当前 UI 树和当前用户消息
（`src/harmony_test_agent/dc/provider.py:48-52`）。

运行快照中的第二轮第一步明确写着没有上一轮任务上下文。这个结论与源代码和
快照状态一致，不是模型自行误判。

现有会话持久化（`src/harmony_test_agent/dc/store.py:65-75`）解决了正常结束后
的历史列表和恢复，但还没有解决“在取消、超时或进程崩溃时，把可继续的信息
及时记录下来并注入下一轮”的问题。

## 4. 目标行为

修复完成后，DC 会话必须满足以下行为：

1. 工具刚开始执行时，右侧操作日志立即显示一条 `running` 记录，并显示工具名、
   调用 ID、参数摘要、已耗时和当前阶段。
2. 工具结束时，原记录按同一个 `invocation_id` 更新为 `succeeded`、
   `failed`、`timed_out`、`cancelled` 或 `unknown`，不能新增重复卡片。
3. 在等待模型、截图、UI 层级或 HDC 命令期间，前端至少每秒收到或本地推导出
   可见进度；超过告警阈值时显示“仍在执行”和已耗时。
4. 模型调用使用配置的单次超时和轮次总超时。超时后发出明确事件，轮次进入
   `failed`、`cancelled` 或 `needs_attention`，不能无限等待。
5. 取消或服务异常后，下一条用户消息仍能看到上一轮的原始用户目标、已完成操作、
   最后已确认界面、最后公开叙述和未决状态。用户说“继续”时，Agent 先基于当前
   状态确认，再决定下一步，不得直接回复“没有上下文”。
6. 已完成的副作用可以作为历史证据展示；副作用未知的动作不能自动重放，必须
   先刷新截图、UI 层级和前台应用，必要时进入 `needs_attention`。
7. 页面刷新、SSE 断线、服务重启和历史会话恢复后，操作日志、聊天记录、截图和
   当前状态仍然一致。

## 5. 推荐数据模型和状态机

### 5.1 工具调用状态

在保持现有 API 向后兼容的前提下，为 `DcToolInvocation` 增加明确状态：

```text
running
succeeded
failed
timed_out
cancelled
unknown
```

旧的 `success: bool` 可以保留作为兼容字段，但新代码不能再用
`success=True` 表示“尚未结束”。未结束记录必须具有：

- `invocation_id`
- `turn_id`
- `tool`
- `args`
- `started_at`
- `status`
- `last_progress_at`
- `deadline_at`
- `effect_status`: `none | confirmed | unknown`
- `command_id` 或底层执行标识

### 5.2 事件信封

所有新增事件至少携带：

```json
{
  "schema_version": 2,
  "session_id": "dc-...",
  "turn_id": "turn-...",
  "event_id": 123,
  "sequence": 17,
  "phase": "tool",
  "operation_id": "inv-...",
  "timestamp": "2026-09-15T09:18:23.182Z",
  "elapsed_ms": 0,
  "payload": {}
}
```

建议新增或统一以下业务事件：

- `context_capture_started`
- `context_capture_finished`
- `model_call_started`
- `model_call_progress`
- `model_call_finished`
- `model_call_failed`
- `tool_call_started`
- `tool_call_progress`
- `tool_call_finished`
- `turn_cancel_requested`
- `turn_interrupted`
- `turn_finished`
- `context_checkpoint`
- `needs_attention`

现有 `thinking` 和 `agent_text` 继续兼容，但 `thinking` 只能代表经过策略允许的
公开阶段信息或摘要，不能无条件转发供应商隐藏推理全文。

### 5.3 轮次状态机

```text
idle
  -> turn_started
  -> capturing_context
  -> waiting_model
  -> model_output_received
  -> tool_running
  -> observing
  -> waiting_model
  -> completed

capturing_context / waiting_model / tool_running / observing
  -> cancel_requested
  -> cancelled

waiting_model
  -> model_timeout
  -> failed

tool_running
  -> tool_timeout
  -> reconciling
  -> completed / failed / needs_attention

任意活动状态
  -> process_interrupted
  -> interrupted
  -> 下次消息前先恢复上下文并重新观测
```

`cancelled` 表示用户或系统已请求终止本轮；`interrupted` 表示执行环境中断，
例如 API 进程重启。两者都必须保存上下文，但只有确认设备副作用后才能继续自动
推进。

## 6. 分阶段实施计划

### Phase 0：建立可复现基线

实现 Agent 先固定以下基线，不改业务目标：

1. 用同一设备和相同 DC 会话配置重放“打开日历并新建事件”任务。
2. 保存 API、Web、HDC、SSE 和 `dc_session.json` 的绝对时间戳。
3. 在截图、UI 层级、模型调用和工具调用四个层级分别记录开始、结束、错误和
   调用 ID。
4. 明确记录 Web 是否在运行期间收到 `tool_call_started`，以及操作日志读取的
   `session.invocations` 是否仍为 0。

该阶段的产物必须能回答：每一个静默区间发生在模型、工具、设备、事件总线还是
前端状态更新。

### Phase 1：修复操作日志的数据源和实时状态

涉及文件：

- `src/harmony_test_agent/dc/models.py`
- `src/harmony_test_agent/dc/tools.py`
- `src/harmony_test_agent/dc/session.py`
- `web/src/api/dc-types.ts`
- `web/src/stores/dc-console.ts`
- `web/src/features/dc/DcOperationLog.tsx`
- `web/src/styles/global.css`

实施要求：

1. `DcActionRecorder.run()` 在发出 `tool_call_started` 之前创建
   `running` invocation 并加入 recorder；完成时就地更新该对象。
2. `tool_call_started` 事件携带完整的最小公开记录：调用 ID、轮次 ID、工具名、
   参数摘要、开始时间和截止时间。
3. `tool_call_finished` 事件携带最终状态、耗时、结果摘要、错误分类和副作用
   状态；同一调用只能有一个最终状态。
4. `DcSession.to_view()` 立即能看到运行中的 invocation；在正常完成、失败、取消、
   超时和异常路径都为 `turn.invocation_ids` 写入当前轮次的全部调用。
5. 前端 store 增加以 `invocation_id` 为键的操作账本和顺序列表。初始
   `session.invocations` 是快照，SSE 是实时补丁；两者合并时以调用 ID 去重。
6. 收到 `tool_call_started` 后立即显示操作行；收到 finished 后更新同一行。
7. `turn_finished`、`error`、SSE 重连成功和历史恢复完成后执行一次 GET 对账，
   但对账结果不能把更新中的实时记录覆盖掉。
8. `DcOperationLog` 不再把“session 仍在加载”或“正在同步”显示成“尚无操作
   记录”。至少区分：
   - 未选择会话；
   - 会话已选择但尚未开始工具；
   - 工具正在执行；
   - 已有历史工具记录；
   - SSE 断线等待对账。
9. 日志面板保留折叠能力，但展开区域固定最小高度、滚动上限和文本截断规则；
   参数详情放到可展开内容，不允许长参数撑破布局。

Phase 1 的最低验收是：工具开始后不需要刷新页面，附图中的 `0 步` 会变为
至少 `1 步`；工具结束后仍为同一调用记录，不重复、不闪烁。

### Phase 2：补齐模型、工具和上下文采集的进度

涉及文件：

- `src/harmony_test_agent/dc/provider.py`
- `src/harmony_test_agent/dc/tools.py`
- `src/harmony_test_agent/dc/session.py`
- `src/harmony_test_agent/dc/hdc.py`
- `src/harmony_test_agent/dc/router.py`
- `web/src/stores/dc-console.ts`
- `web/src/features/dc/DcChat.tsx`
- `web/src/features/dc/use-dc-runtime.ts`

实施要求：

1. 在 Provider 每次模型请求开始前发出 `model_call_started`，携带
   `model_call_id`、阶段、尝试号、单次 deadline 和轮次剩余预算。
2. 消费 `agent.iter()` 时对每次推进使用剩余 deadline；不能只包住外层任务，
   也不能让多个内部重试无限叠加。模型请求超时要发出 `model_call_failed`，
   error code 为 `model_timeout`。
3. DC 路径实际使用 `settings.agent_model_timeout`。如需单独配置
   `dc_turn_timeout`，必须明确优先级：单次模型超时 <= 轮次总超时。
4. Provider 不支持 token streaming 时，仍必须发出 started、progress heartbeat、
   validating、finished/failed 事件。不能把“没有 token 流”变成“没有任何进度”。
5. 工具执行每隔固定间隔发出 `tool_call_progress`。进度至少包括已耗时、剩余
   时间、当前阶段和可取消性；不要伪造不存在的百分比。
6. 截图工具拆分记录：
   `snapshot_display`、`file_recv`、图片解码、远端临时文件清理。当前运行中
   截图 8 到 16 秒，必须能看出具体慢在哪一步。
7. `DcHdcExecutor._run()` 与工具层超时不能形成互相矛盾的两个 deadline。
   优先让底层 subprocess 拥有可终止的进程句柄、进程组和 timeout；上层取消时
   尝试终止并等待短暂收尾。无法确认终止时，记录 `unknown`，不得把它标成
   普通失败后立即重放。
8. 前端增加当前活动条，显示：
   - `正在采集截图`
   - `正在读取 UI 层级`
   - `正在等待模型`
   - `正在执行 screenshot`
   - `正在等待设备响应`
   - `正在校验结果`
   后面附实时耗时、最后心跳和明确错误。
9. 超过告警阈值时显示“仍在执行”，超过 deadline 时显示超时及处置结果；
   页面不能永久显示三个点动画。
10. 心跳事件进入 SSE 时按批次或固定间隔发送，不能每个 token、每次循环都写
    一次完整快照。慢客户端必须有有界队列和丢弃/断线策略。

推荐初始配置以现有值为基线并全部可配置：

- 单次模型调用：使用现有 `AGENT_MODEL_TIMEOUT`，默认 90 秒；
- 单次普通工具：使用现有 `AGENT_ACTION_TIMEOUT`，默认 30 秒；
- 单轮总预算：新增 `DC_TURN_TIMEOUT`，建议先设为 600 秒；
- 进度心跳：1 秒到 2 秒；
- UI 告警阈值：5 秒；
- SSE/Trace 公开文本和结果摘要：分别设置字节上限。

这些数字是实施起点，不是允许无限延长的理由。实机重放必须证明超时边界真实
生效。

### Phase 3：修复取消后的上下文连续性

涉及文件：

- `src/harmony_test_agent/dc/models.py`
- `src/harmony_test_agent/dc/session.py`
- `src/harmony_test_agent/dc/store.py`
- `src/harmony_test_agent/dc/provider.py`
- `src/harmony_test_agent/dc/router.py`
- `web/src/api/dc-types.ts`
- `web/src/stores/dc-console.ts`
- `web/src/features/dc/DcPanel.tsx`

新增一个与 Pydantic AI 原始消息分离的公开连续性摘要，例如：

```text
ContinuationContext
  previous_turn_id
  previous_status
  original_user_goal
  public_agent_summary
  public_agent_steps
  completed_operations[]
  active_or_unknown_operation
  last_snapshot_path
  last_page_path
  last_foreground_app
  last_progress_at
  effect_status
  reconcile_required
  context_version
```

实施要求：

1. 新轮次 prompt 总是包含最近若干轮的公开连续性摘要，不依赖
   `response.history` 是否成功返回。
2. 对于 `completed` 轮次，摘要包含目标、完成结果、工具序列和最后已确认状态。
3. 对于 `cancelled`、`interrupted`、`failed` 或 `needs_attention` 轮次，摘要必须
   包含原始目标、已完成工具、最后公开步骤、失败点和“下一步先重新观测”的约束。
4. `thinking` 原文不进入连续性摘要和模型历史；使用用户消息、工具记录、公开
   `agent_text`、阶段状态和脱敏决策摘要。
5. 对用户消息“继续”“现在继续”“刚才卡住了继续”等自然语言，不依赖硬编码
   关键词恢复上下文。上下文由会话状态统一注入，模型负责理解当前消息和摘要
   的关系。
6. 新任务仍然可以覆盖旧任务目标，但旧任务状态不能被删除。模型必须能区分：
   - 继续上一轮；
   - 修正上一轮；
   - 开始一个无关的新任务。
7. 取消分支和异常分支在返回前都更新 `turn.invocation_ids`，保存公开连续性
   摘要，并发出 `turn_interrupted` 或 `turn_finished(status=cancelled)`。
8. 每次工具开始、工具结束、模型阶段完成、轮次取消都写一个有界 checkpoint。
   checkpoint 使用现有原子替换方案，但写盘必须避免阻塞事件循环，可采用合并、
   版本号和后台写入。
9. 服务重启恢复时，不能把历史截图当作当前设备状态。恢复后先执行一次新的
   `screenshot`、`foreground_app` 或 UI 层级采集，再决定是否继续。
10. 如果上一次动作的副作用未知，恢复接口返回 `needs_attention` 或等价状态，
    提供“重新观测”“确认已生效并继续”“确认未生效后重试”“终止”选项。

针对本次运行，第二轮的模型输入至少应该能看到：

- 第一轮原始目标：打开日历、切换月视图、选择 9/18、创建 8 点到 9 点的纪念事件；
- 已完成的打开应用、搜索、切换月视图、选择日期和打开新建日程 Sheet；
- 最后已知界面是日历的新建日程表单，标题仍为空；
- 第一轮在读取表单 UI 时被取消，尚未完成事件创建；
- 下一步应先重新采集当前界面，再定位标题、时间和保存控件；
- 不应把剪贴板中的英文残片当成用户要求的标题。

### Phase 4：取消、超时和恢复的设备安全

这一步不能只把异常文本显示到前端，必须处理设备副作用：

1. `stop_turn` 先设置取消标记，再请求取消任务；不要只调用
   `task.cancel()`。
2. 取消后的 HDC worker 必须被等待、终止或标记为 `unknown`。在结果未知时，
   持续持有设备轮次锁，直到完成收尾或进入人工处理状态。
3. 新轮次在旧轮次 `reconciling` 时不能直接驱动设备；返回明确的 409 或
   `needs_attention` 事件。
4. 只能对无副作用或已确认未生效的操作自动重试。点击、输入、按键、滑动、
   启动/停止应用等操作在超时后默认 `effect_status=unknown`。
5. 恢复或继续前重新采集截图、前台应用和必要的 UI 层级；旧的坐标、旧的
   bbox、旧的页面快照和旧的模型决策不能直接重放。
6. 记录设备租约、执行代次和 `context_version`，防止旧 worker 在新轮次中写入
   错误的 finished 事件。
7. 会话关闭、服务 shutdown 和空闲淘汰都必须等待有限时间并保存终态；超出等待
   后明确标为 `interrupted`，不能伪装为 `closed`。

## 7. 前端 UI 修复要求

### 操作日志

右侧日志面板应从“静态历史列表”改为“历史 + 实时执行账本”：

- 标题计数统计 `running`、终态和 `unknown` 的总数；
- running 行显示 spinner、工具名、开始时间和动态耗时；
- succeeded 使用成功色，failed/timed_out/unknown 使用不同状态；
- 每行可展开参数、结果摘要、错误和关联截图；
- 空状态只在确认没有历史和实时调用时显示；
- 数据同步期间显示同步状态，不显示“尚无操作记录”；
- 断线时保留已有行，并显示最后事件 ID和重新连接状态；
- 会话切换时清理旧会话的实时账本，避免跨会话串行。

### 当前活动区

聊天面板应保留用户消息、公开 Agent 叙述、工具卡和最终总结，但另加一个
独立的当前活动区。它不依赖模型是否产生文本，至少能显示：

```text
阶段：正在等待模型
调用：model-call-...
已耗时：01:32
最后进度：09:18:35
状态：将在 90 秒时超时
```

停止按钮必须显示“请求停止”和“正在确认设备状态”两个阶段，直到旧 worker
确实结束或进入 `unknown`。

### 消息去重和刷新

事件增量和 GET 全量快照必须使用同一个 ID 规则：

- 事件按 `event_id` 去重；
- 工具按 `invocation_id` 就地更新；
- 轮次按 `turn_id` 合并；
- 公开模型步骤按 `step_id` 或稳定序号去重；
- refresh 只能补缺失信息，不能清空正在显示的 running 行；
- SSE 重连后先按 Last-Event-ID 回放，再以服务端快照对账；
- 会话切换后禁止旧 SSE 回调更新新会话状态。

## 8. 测试和验收清单

### Python 单元和 API 测试

新增或扩展以下测试类别：

1. `tool_call_started` 后，session view 立即含一条 `running` invocation。
2. `tool_call_finished` 更新同一个 invocation ID，不新增第二条。
3. 工具失败、超时、取消和未知副作用分别产生正确状态。
4. 第一轮在模型调用期间取消，快照仍保存用户目标、公开步骤和已有调用。
5. 第一轮在工具中途取消，`turn.invocation_ids` 与全局 invocations 一致。
6. `AGENT_MODEL_TIMEOUT` 对 DC Provider 生效，超时前有 started/heartbeat，超时后
   有 failed/finished，不会无限等待。
7. `DC_TURN_TIMEOUT` 能终止多次模型和工具调用叠加的长轮次。
8. 取消后的下一轮请求包含原始目标、已完成工具和最后状态；不包含隐藏
   `ThinkingPart`。
9. 恢复历史会话后先标记需要重新观测，不能直接重放旧坐标或旧决策。
10. 服务重启、损坏快照、旧 schema、半写入临时文件、缺少截图和缺少设备时，
    列表、恢复和错误状态均可解释。
11. 两个会话共用一个设备时，旧轮次处于 `unknown/reconciling` 期间，新轮次被
    明确阻塞，不会静默排队或抢占设备。
12. SSE replay、重复 finished、finished 先于 started、事件乱序和事件丢失时，
    前端和后端都保持幂等。

### Web 测试

1. 仅收到 `tool_call_started` 时，操作日志立即显示 `1 步` 和 running。
2. 收到 finished 时同一行变为终态，计数不变，聊天工具卡与日志状态一致。
3. 初始 session 快照为空、随后只有 SSE 事件时，日志仍能显示调用。
4. refreshSession 在 running 期间不会覆盖实时调用；完成后对账补齐完整字段。
5. 断线重连、事件重复、会话切换和旧回调到达时不串数据。
6. 长时间没有模型文本时，当前活动区仍显示阶段、耗时和超时倒计时。
7. 取消、超时、unknown 和 needs_attention 都有明确视觉状态，停止按钮不会
   永久转圈。
8. 小屏和宽屏下，日志参数、错误和活动状态不会溢出、重叠或把聊天区挤出
   可视区域。

### 实机验收

至少完成以下三组，每组保存 API/SSE/快照/截图证据：

1. 正常完成任务：日志从 running 到 succeeded，聊天和 session view 一致。
2. 在最后一个截图后故意等待或注入慢模型，确认每秒有心跳，并在配置的
   90 秒模型 deadline 前后得到明确状态。
3. 在新建日程 Sheet 已打开后取消第一轮，然后发送“你刚刚在执行什么卡了这么久，
   现在继续”。确认 Agent 能复述原始目标和已完成步骤，先重新观测当前界面，
   不误把剪贴板残片当标题，也不重复已经生效的点击。

实机验收不能用 Mock Provider、静态单测或历史截图替代。必须保存：

- 会话 ID、轮次 ID、事件 ID范围；
- 每个模型和工具调用的开始/结束时间；
- SSE 断线与恢复记录；
- `dc_session.json` 最终内容；
- 失败/取消/unknown 时的最后前后截图；
- 若发生 `needs_attention`，保存用户处置和后续状态。

## 9. 实施顺序和变更边界

建议按以下顺序提交，便于逐阶段回滚和定位：

1. `fix(dc): synchronize live operation ledger`
2. `fix(dc): add model and tool progress deadlines`
3. `fix(dc): preserve continuation context after interruption`
4. `fix(dc): reconcile cancelled device effects`
5. 对应 Python、Web、API、SSE 和实机验收测试

本任务只修改 DC Mode 相关路径和必要的共享配置/测试。不得把修复扩散到
Live Mode 的 `AgentOrchestrator`、`runtime/*`、既有 Live SSE 或 Hypium 生成
逻辑，除非实现 Agent 能证明存在跨模式公共契约且先补回归测试。

## 10. 方案审问结果

实施前必须保持以下判断：

1. **是否把原始思考全文展示给用户？** 不展示。公开阶段和脱敏决策摘要足够
   解释运行状态，隐藏推理不得进入历史上下文。
2. **工具超时后是否直接重试？** 不直接重试。先判断设备副作用；未知时先
   reconcile 或进入 `needs_attention`。
3. **日志的唯一来源是什么？** 服务端 invocation ledger 是事实来源，SSE 是
   增量传输，GET 快照是对账来源，聊天消息不是操作日志的替代品。
4. **进程重启后是否自动继续旧动作？** 不自动重放未确认动作。只恢复会话事实，
   重新观测设备后才决定继续。
5. **用户发送新消息是否新建一段完全孤立的上下文？** 不孤立。每条新消息都
   创建新 turn，但通过公开连续性摘要继承最近历史。
6. **用户发送无关新任务怎么办？** 保留旧任务记录，将新消息作为新目标；模型
   只能在当前设备状态确认后执行。
7. **UI 显示 `0 步` 是否能说明后端没有执行工具？** 不能。必须同时查看实时
   invocation ledger、事件流和 session 快照。
8. **没有 token streaming 是否意味着必须等待无输出？** 不是。即使 Provider
   不支持 token 流，也必须提供阶段事件、心跳、耗时、超时和最终结果。

## 11. 完成定义

只有同时满足以下条件才可称为完成：

- 附图所示操作日志在工具开始后实时出现，并能正确显示终态；
- 长模型等待不再出现超过 deadline 的静默区间；
- 取消或中断后发送跟进消息，Agent 能使用原始目标和已完成状态继续；
- 未确认副作用不会被盲目重放；
- 页面刷新、SSE 重连、服务重启和历史恢复后数据一致；
- 对应 Python 和 Web 测试通过；
- 最新 DC 实机三组验收有保存证据；
- 按仓库 `AGENTS.md` 完成要求的静态检查、测试、构建和 `git diff --check`；
- 完成报告明确列出分支、文件、检查结果、未验证项和是否合并到 `main`。

## 12. 追加归因：run `dc-20260916T160556Z-d23f9684`（`UsageLimitExceeded: request_limit of 30`）

### 12.1 现象与证据

证据来自 `artifacts/runs/dc-20260916T160556Z-d23f9684/dc_session.json`。

| 事实 | 值 |
| --- | --- |
| session / turn | `dc-20260916T160556Z-d23f9684` / `turn-3fc7f2f0` |
| 用户目标 | 打开日历 → 切月视图 → 打开 9 月 22 日 → 为该日创建生日日程（提示时间下午 1 点）→ 返回日历首页 |
| 轮次起止 | `16:07:07Z` → `16:14:05Z`（7 分 58 秒） |
| 轮次状态 | `failed` |
| `turn.error` | `UsageLimitExceeded: The next request would exceed the request_limit of 30` |
| 工具调用 | 正好 30 次且全部 `succeeded`：`screenshot`×14、`click`×7、`swipe`×6、`list_apps`×1、`dump_ui_hierarchy`×1、`wait`×1 |
| 模型步骤 | 22 条 `thinking`，0 条 `agent_text`，无终态回答 |
| UI 层级规模 | 打开「新建日程」后 `103~105 elements`，摘要只展示 `top 60` |
| 平均单请求耗时 | ≈14 秒（最长空档 37 秒；`AGENT_MODEL_TIMEOUT=90` 从未触发） |

### 12.2 根因链

1. `request_limit=30` 统计的是 **pydantic-ai 的模型 HTTP 请求次数**
   （`.venv/.../pydantic_ai/_agent_graph.py:804` 每次模型请求前调用
   `UsageLimits.check_before_request`），而 DC 系统提示词强制「每轮一个工具」，
   于是 **1 次请求 = 1 次工具往返**。预算在 30 次工具往返后耗尽，第 31 次请求被拒。
2. 该上限当时是 `dc/provider.py` 里的硬编码字面量，不可配置，也没有任何地方
   说明它和 `DC_TURN_TIMEOUT` 的关系。
3. 30 次预算之所以会用光，是因为模型在**盲操作**：`dc/session.py` 注入 prompt 的
   UI 树摘要按**树的前 `dc_ui_tree_top_k`（当时默认 60）个元素**截断；该页 105 个
   元素中，导航栏 + 侧边栏 + 背景月历占了前约 52 个，底部弹窗表单的字段、类型页签
   文案、确定按钮全部落在第 60 名之后。模型自己的思考原文即为此：
   "the 60-element cap hides the form"、"without visibility this is guesswork"。
   于是它用 14 次截图 + 7 次盲点坐标反复试探，预算耗尽时连日历都没打开成功。
4. 异常处理缺口：`provider.chat()` 的推进循环只捕获
   `StopAsyncIteration / TimeoutError / CancelledError`，`UsageLimitExceeded`
   裸穿（且抛点在 `_advance_with_progress` 之前，连 `MODEL_CALL_FAILED` 都没发），
   最后由 `session.py` 的兜底 `except Exception` 写成
   `f"{type(exc).__name__}: {exc}"` —— 用户看到的是英文裸异常，既无处置提示，
   前端「当前活动区」也没有任何失败迹象。

### 12.3 修复

- `config.py`：新增 `DC_MODEL_REQUEST_LIMIT`（默认 120）与
  `DC_MODEL_TOOL_CALLS_LIMIT`（默认 200）；删除从未被读取、语义与真实预算不一致的
  死配置 `dc_max_turn_steps`；`dc_ui_tree_top_k` 默认 60 → 200。
- `dc/provider.py`：上限改为读配置；`UsageLimitExceeded` 转为
  `DcUsageLimitReached` 并补发 `MODEL_CALL_FAILED(error_code="usage_limit")`；
  系统提示词新增「先看清再动手」规则，明确禁止连续截图同一界面与盲点推测坐标。
- `dc/models.py`：新增 `DcUsageLimitReached`（携带命中的 `request_limit` /
  `tool_calls_limit`）。
- `dc/session.py`：该异常归为 `blocked`，`turn.error` 为中文可读文案并点名可调大的
  环境变量，`ERROR` 事件带 `error_code=usage_limit`。
- `web/src/stores/dc-console.ts`：`model_call_failed(error_code=usage_limit)` 映射为
  专用阶段文案「本轮请求预算已用尽」。

### 12.4 复现与核对方式

```powershell
.\.venv\Scripts\python.exe -X utf8 -c "import json,collections;d=json.load(open(r'artifacts/runs/dc-20260916T160556Z-d23f9684/dc_session.json',encoding='utf-8'));print(collections.Counter(i['tool'] for i in d['invocations']));print(d['turns'][0]['error'])"
```

预期输出：工具计数共 30 次、`turn.error` 为上述 `UsageLimitExceeded` 文案。

用该 run 保存的真实 UI dump 复算 top-K 截断（`layouts/dc_1789575210_snap-489040651c22.json`，
共 105 个元素）可以量化「模型看不见什么」：

```powershell
.\.venv\Scripts\python.exe -X utf8 -c "import json;from harmony_test_agent.perception.normalizer import normalize_layout;h=json.load(open(r'artifacts/runs/dc-20260916T160556Z-d23f9684/layouts/dc_1789575210_snap-489040651c22.json',encoding='utf-8'));els=normalize_layout(h,1320,2232);print('total',len(els));print('top60 last=',els[59].type,els[59].key);print('hidden clickable/editable@60:',len([e for e in els[60:] if e.clickable or e.editable]))"
```

实测结果：

- `top 60` 的最后一个元素是 `Column key='tab_controller'`，即摘要恰好在类型页签那一行被切断；
- `top_k=60` 时**完全不可见**的可交互元素有 **14 个**，全部是本次任务真正要操作的表单控件：
  `tabs_schedule`（日程）/ `tabs_important_event`（重要日）、`add_agenda_location`、
  `全天` 行与其 `Toggle`、`add_agenda_start_time`、`add_agenda_end_time`、
  `add_agenda_add_remind`（添加提醒）、`重要提醒` 行与其 `Toggle`、
  `add_agenda_select_account`、`agenda_remark`，以及底部按钮行；
- `top_k=200` 时 105 个元素全部进入摘要。

也就是说，模型不是「不会操作」，而是「看不到要操作的东西」——这正是它把 30 次预算
全部烧在截图与盲点坐标上的原因，也解释了 12.3 中提高 `DC_UI_TREE_TOP_K` 的必要性。

**未验证项**：修复后的实机重放（同一日历任务）尚未执行；默认 120/200 的充分性需要
下一次真实 run 的证据，若仍不足则按实测调大（已配置化，无需改代码）。

## 13. Token 用量与缓存命中率：数据源修复与展示

### 13.1 上游丢数据的两个原因（实测）

自建 OpenAI 兼容端点（`commandcode.ai`）返回的 usage 是完整的：

```json
"usage": {
  "prompt_tokens": 1721, "completion_tokens": 16, "total_tokens": 1737,
  "prompt_tokens_details": {"cached_tokens": 1536, "audio_tokens": 0, "video_tokens": 0},
  "completion_tokens_details": {"reasoning_tokens": 16, "image_tokens": 0},
  "cache_creation_input_tokens": 0
}
```

但 `pydantic-ai 1.73.0` 的 `Agent.run().usage()` 返回的 `RunUsage` 里
`input_tokens / output_tokens / cache_read_tokens` **全是 0**，原因有两个：

1. `OpenAIChatModel._map_usage` 只保留顶层且 `isinstance(v, int)` 的字段，
   `prompt_tokens_details` 是嵌套 dict 被直接丢弃 → 缓存命中数永远为 0；
2. 真正赋值 input/output tokens 的 `RequestUsage.extract()` 依赖 genai-prices 的
   provider 快照，自建端点不在快照里，兜底到 `openai` 后一个字段都没提取到
   → token 数永远为 0。

**结论：不改 provider 就做不出用量展示（数据源是 0）。**

### 13.2 缓存命中率的分母（实测语义）

共享 1721 token 前缀连发两次请求：

| | `prompt_tokens` | `cached_tokens` |
| --- | --- | --- |
| 冷启 | 1721 | 0 |
| 热启 | 1721 | 1536 |

`prompt_tokens` **包含**缓存命中部分（`1721 - 1536 = 185` 恰为新增部分），因此：

```
缓存命中率 = cached_tokens / prompt_tokens = cache_read_tokens / input_tokens
```

用 `input + cached` 作分母会得到 47% 这种系统性低估的错误值，测试中已锁定该语义。

### 13.3 实现

- `agents/providers.py`：新增惰性定义的 `OpenAIChatModel` 子类（只覆盖上游留作扩展点的
  `_map_usage`），直接从响应对象取 `prompt_tokens` / `completion_tokens` /
  `prompt_tokens_details.cached_tokens` / `cache_creation_input_tokens`，不再经过
  genai-prices；没有 `usage` 时委托 `super()`。DC 与 Live 模式的用量口径同时被修正。
- `dc/models.py`：新增 `DcTokenUsage`（含 `cache_hit_rate`、`plus`、`minus`、
  `from_run_usage`）、事件 `token_usage_updated`、`DcChatResponse.usage/usage_delta`、
  `DcSessionView/DcSessionSnapshot.token_usage`。
- `dc/provider.py`：在 `agent.iter()` 上下文内读取 `run.usage()`（块外不可用），
  以「本次累计 − 上次累计」得到本轮增量；超时/取消/预算耗尽路径用块内快照兜底，
  增量带 `turn_id` 标记，避免上一轮的增量被重复入账。
- `dc/session.py`：`self.token_usage` 累计并在 `TOKEN_USAGE_UPDATED` 事件里下发权威值
  （前端整值覆盖，SSE 重连重复投递天然幂等）。
- 前端：`DcTokenStatsBar` 挂在输入框下方，显示本会话总量、缓存命中率、输入/输出/缓存读、
  请求进度与（仅在单请求轮次）上下文占用率；无数据显示「尚无用量数据」。
- 配置：新增 `DC_MODEL_CONTEXT_WINDOW`（默认 128000），仅用于展示占用比例。

### 13.4 复现

```powershell
.\.venv\Scripts\python.exe -X utf8 -c "import asyncio,sys,httpx;sys.path.insert(0,'src');from harmony_test_agent.config import get_settings;s=get_settings();r=httpx.post(s.openai_base_url.rstrip('/')+'/chat/completions',json={'model':s.agent_model,'messages':[{'role':'user','content':'hi'}],'max_tokens':8},headers={'Authorization':'Bearer '+s.openai_api_key.get_secret_value()},timeout=90);print(r.json()['usage'])"
```

**未验证项**：真实会话下统计行的实机显示（需要设备 + 模型）尚未执行；数字口径已由
provider 层单测锁定（`tests/unit/test_provider.py`）。
