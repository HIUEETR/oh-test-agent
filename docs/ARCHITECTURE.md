# OpenHarmony 多模态测试 Agent 架构

更新日期：2026-09-13

## 1. 架构目标

系统不是“让大模型直接操作 Shell”，而是“一个多模态 Agent + 确定性执行框架”：

- LLM 把自然语言任务转换为 `PlannedStep`；
- VLM 接收实际截图与 UI 层级，输出结构化页面理解；
- 模型每轮只返回一个 `ToolDecision`；
- 执行框架校验工具、语义、坐标、超时和停止状态；
- HDC 只存在于类型化 `HarmonyDeviceAdapter` 内；
- 所有动作保存证据；
- Hypium 只从成功且已验证的 Action 生成。

## 2. 模块结构

```text
src/harmony_test_agent/
├─ agents/
│  ├─ providers.py       OpenAI-compatible / Mock Provider
│  └─ orchestrator.py    状态机、计划循环、截图、图和生成编排
├─ api/
│  └─ app.py             FastAPI、SSE、RunManager 和产物查询
├─ devices/
│  ├─ base.py            DeviceAdapter 抽象
│  └─ harmony.py         HDC 命令、截图、布局、操作和日志
├─ discovery/
│  ├─ advisor.py         连续会话式 LLM 视觉探索顾问
│  ├─ explorer.py        有界探索、逻辑页去重、内容抑制与恢复节奏
│  ├─ stability.py       跨轮稳定定位器/断言分析
│  └─ verification.py    三轮设备验证与准入门控
├─ generation/
│  └─ hypium.py          模板化 Hypium Python/JSON/元数据
├─ graph/
│  └─ page_graph.py      页面签名、节点去重和动作边
├─ perception/
│  ├─ normalizer.py      bounds、元素过滤、语义定位
│  └─ service.py         UI 层级与 VLM 元素融合
├─ runner/
│  └─ hypium.py          隔离环境、子进程、回放证据
├─ runtime/
│  ├─ safety.py          危险语义、坐标和等待限制
│  ├─ tools.py           白名单工具到设备操作的映射
│  └─ events.py          RunEvent 持久化
├─ storage/
│  ├─ artifacts.py       JSON/PNG/日志/报告目录
│  └─ repository.py      SQLite Run 与事件索引
├─ models.py             Pydantic 领域模型
├─ preflight.py          环境、设备、截图和 Hypium 门禁
├─ reporting.py          HTML/JSON 报告
├─ config.py             `.env` 配置与脱敏
└─ cli.py                preflight/run/generate/execute/serve
```

## 3. 领域模型

核心模型统一位于 `models.py`：

- `TargetAppProfile`：应用标识、启动/重置策略、测试数据策略和人工确认的稳定定位器。
- `ScreenSnapshot`：截图路径、SHA-256、尺寸、布局路径、页面摘要和动态元素。
- `UIElement`：内容、类型、bbox、key/id、交互属性、来源、置信度和定位候选。
- `VisionObservation`：VLM 页面标题、摘要和视觉元素。
- `PlannedStep`：计划工具、目标、输入、方向、等待和预期。
- `ToolDecision`：模型当前轮选择的唯一工具。
- `ActionResult`：实际工具、参数、前后 Snapshot、命令、定位器、断言、耗时和错误。
- `RunTrace`：任务、计划、Snapshot、Action、断言、页面图、事件、生成文件和回放结果。
- `RunEvent`：严格递增事件 ID、类型、时间、消息和 payload。

`TargetAppProfile.stable_locator_inventory` 只保存人工确认的稳定 key/id。运行时 `element_id`、动态卡片 ID、VLM bbox 和页面文本只进入 Snapshot/Trace，不回写 Profile。

## 4. 状态机

正常路径：

```text
created
→ preflight
→ planning
→ executing
→ verifying
→ graph_updating
→ script_generating
→ script_executing（可选）
→ completed
```

终止状态：

```text
failed_device
failed_model
failed_element
failed_action
failed_assertion
failed_script
stopped_by_user
```

失败状态一旦产生，后续步骤不再执行。

## 5. 单步执行链路

```text
当前本地截图 + UI 层级
→ VLM 结构化分析
→ Provider.decide 返回一个 ToolDecision
→ finish 门禁与参数模型校验
→ SafetyPolicy 校验危险语义/坐标/等待
→ ToolExecutor 调用 DeviceAdapter
→ 等待界面稳定
→ 再次截图与 VLM 分析
→ 断言、页面节点和动作边
→ ActionResult、RunEvent、SQLite、trace.json
```

每个 Action 都必须有前后 Snapshot。即使是 `finish`，框架也会采集最终截图作为结束证据。

动作与截图之间有两级等待：`open_app` 动作进入**冷启动静默期**（编排器参数 `launch_settle_seconds`，默认 3 秒）——期间轮询 `current_foreground_app` 确认前台已切到目标应用，并保证总静默满预算后再截图，避免把启动 logo 页当作首页；其余动作等待固定的 `settle_seconds`（默认 0.8 秒）。截图本身由 `_capture_stable_frame` 执行：在 `exploration_policy.settle_timeout_seconds` 预算内轮询 UI 层级指纹，静止页返回首帧，持续变化页（加载动画/过渡帧）在预算耗尽后补截 `-settled` 帧；探索阶段由 `BoundedExplorer._capture_settled` 提供同语义轮询。

## 6. Planning 与真实模型兼容

### 6.1 结构化输出

- Planning、Vision、Tool Decision 与探索顾问统一使用 `PromptedOutput`（`OpenAICompatibleProvider._structured_output`），不注册工具、不发送 `tool_choice`，规避 thinking 端点对强制 tool choice 的拒绝。
- `AGENT_DISABLE_THINKING=true` 时追加 `extra_body.thinking.type=disabled`；该字段只对 DeepSeek 官方端点有效，对 OpenAI 兼容自建端点会被忽略，因此不能作为 `tool_choice` 冲突的兜底。
- 规划、截图分析、工具决策统一使用 `AGENT_MODEL_TIMEOUT`；HDC 动作使用独立的 `AGENT_ACTION_TIMEOUT`。

### 6.2 计划对齐

模型计划后执行确定性对齐：

- 用户只要求输入文本时，不允许模型擅自增加“提交搜索”和“验证搜索结果”；
- 用户明确要求提交或查看搜索结果时保留该流程；
- 文本输入后返回首页时，确保计划考虑软键盘可能先消费一次返回动作；
- `finish` 永远位于最大步数之内。

### 6.3 finish 门禁

- 非 finish 计划步骤中，模型返回 finish 会被框架替换回计划工具；
- 计划 finish 步骤不能被模型换成其他工具；
- 其他安全的非 finish 自适应仍被保留，例如把返回动作改为点击可见的应用内返回按钮。

### 模型实例与上下文

`AGENT_MODEL` 用于任务规划；`AGENT_VISION_MODEL` 用于截图理解和逐步工具决策。它们通过编排器持久化的 `PlanResult`、`ScreenSnapshot`、页面元素和当前 `PlannedStep` 交换结构化数据，没有模型之间的直接会话通道。将 `AGENT_VISION_MODEL` 留空会让视觉阶段回退到 `AGENT_MODEL`，因此可以统一为一个支持图像输入和结构化输出的多模态模型。

统一模型只减少模型切换和提示前缀差异，不等于共享一个连续对话：`plan`、`analyze`、`decide` 各自新建 Pydantic AI `Agent` 并发起独立请求。规划上下文由 `PlanningContext` 承载——`from_profile` 提供 verified Profile 的定位器清单与已知限制，`from_resolved` 仅为实时模式提供解析出的应用身份。例外是探索顾问（`discovery/advisor.py`）：它在单次探索内通过 `advise_turn` 的 `message_history` 维护跨页连续对话（见"探索治理与 LLM 视觉顾问"）。仓库没有 provider prompt-cache 控制或命中统计；实际命中取决于兼容服务是否支持前缀缓存以及请求前缀是否稳定。
## 7. 设备边界

`HarmonyDeviceAdapter` 暴露：

```text
connect
health_check
screenshot
collect_ui_hierarchy
collect_logs
open_app
click
input_text
swipe
back
wait
close
```

它负责：

1. 解析配置或 `PATH` 中的 HDC；
2. 通过 `-t <device>` 固定目标设备；
3. 设备端生成截图；
4. 使用 `hdc file recv` 拉回 Run 目录；
5. Pillow 解码、验证尺寸并转为 PNG；
6. 保存 SHA-256、采集时间和同一 Snapshot 的 UI 层级；
7. 把 stdout、stderr、returncode、超时和耗时转换为 `CommandResult`。

Agent 从未获得 `shell(command)`。

## 8. 工具与安全策略

模型白名单：

```text
inspect_screen
open_app
click_element
click_coordinate
input_text
swipe
back
wait
assert_visible
assert_not_visible
assert_text
finish
```

`SafetyPolicy`：

- 只检查可执行参数 `target` 和 `text`，不因模型解释文本误报；
- 分两层执行：任务描述（`validate_task`）只拦**永久禁用项与凭证词**（支付、删除、卸载、清除数据、密码、验证码等）；"确认/提交/发送/授权"等**门控词**只在工具决策层面（`validate_decision`）按 `allow_login/allow_submit/...` 开关拦截，避免误伤任务描述里的日常用语；
- 坐标必须在当前 Screenshot 内；
- 等待时间有上限；
- 模型不能一次返回多个动作；
- 断言、元素和设备错误不进行危险重试。

## 9. 感知与定位

定位顺序：

```text
stable key/id
> runtime element_id mapped to current Snapshot
> exact text
> type + text
> interactive/spatial relation
> validated VLM bbox
> explicit coordinate
```

`target_variants` 会从“搜索图标/搜索框”“首页界面元素”“内容列表中的一条内容”等自然语言中提取可匹配语义，但精确 `element_id` 优先。

`click_element` 目标解析为两级：先按 `clickable=True` 严格匹配，失败时放宽为任意带边界的匹配元素（滚轮选择器/自绘控件常不标记 clickable，坐标点击仍有效）并写入动作告警；目标带 bbox 的元素也可作为 `swipe` 的锚点——以元素 bbox 中心为轴、行程为元素尺寸一半滑动（滚轮列调值），无 target 时维持屏幕中心滑动。

VLM 元素必须满足：

- bbox 在截图边界内；
- 面积有效；
- 置信度不低于 `VLM_MIN_CONFIDENCE`；
- 与 UI 层级重叠时 UI 层级保持优先。

断言可使用 UI 元素、语义简化或 VLM 页面标题/摘要作为辅助证据，但不会在失败时无限放宽。

## 10. 页面图

页面签名综合：

- 页面路径；
- 稳定控件集合；
- 页面标题/摘要；
- 截图感知信息。

每次状态变化生成节点，Action 生成带工具、目标和置信度的边。当前动态信息流和 VLM 标题变化仍可能导致同一语义页面被拆分成多个状态；这属于已知的过度分裂问题，而不是丢失证据。

## 11. Hypium 生成与回放

生成器只读取 `ActionResult.success=true`：

- key/id → `BY.key` / `BY.id`；
- 动态长数字 key → `MatchPattern.STARTS_WITH`；
- text/type+text → 对应选择器；
- swipe 方向规范化为大写 `UP/DOWN/LEFT/RIGHT`（Hypium 枚举要求），非法值回退 `UP` 并写入生成警告；
- 没有稳定定位器但当前 Snapshot 有 bbox → 坐标中心点，并写入注释和告警；
- 断言 → `check_component_exist`；
- 应用重置策略 → stop/start；
- Python 主体来自固定模板，不执行模型自由代码。

Runner 为每次回放建立 `attempt-NN` 目录，设置项目内 `HOME`/`USERPROFILE`，捕获命令、白名单环境、返回码、stdout、stderr、截图和结果 JSON。

## 12. 存储

```text
artifacts/agent.db                     Run 与事件索引
artifacts/preflight/                   预检与 Hypium 基线
artifacts/runs/<run-id>/trace.json     完整领域对象
artifacts/runs/<run-id>/screens/       前后 PNG
artifacts/runs/<run-id>/layouts/       UI 层级 JSON
artifacts/runs/<run-id>/commands/      HDC 命令结果
artifacts/runs/<run-id>/generated/     Hypium Python/JSON/元数据
artifacts/runs/<run-id>/hypium/        多次回放证据
artifacts/runs/<run-id>/reports/       HTML/JSON 报告
```

SQLite 用于查询和 SSE；JSON/PNG/日志是可移植证据。它们全部被 Git 忽略，只有 `artifacts/README.md` 进入版本管理。

## 13. API 与 Web

FastAPI 提供健康、设备、Run 创建/停止/查询、SSE、Graph、Script、Report、Generate、Execute 和安全产物下载。

SSE 从 SQLite 按事件 ID 增量读取，终止状态且没有新事件后关闭。Web 控制台（`web/`，2026-09 完全重写；旧版冻结于 `web-legacy/`）基于 React 19 + TypeScript + Vite + zustand，按 feature 分层（launcher/live/advisor/graph/script/profiles/report/runs/pipeline），API 访问集中在 `src/api/`（fetch 封装 + EventSource 封装），业务状态集中在 `src/stores/console.ts`。

控制台用 EventSource 订阅全部事件类型并按 event_id 去重，同时串行轮询 Run Trace 与 discovery 快照；`src/utils/thought-aggregator.ts` 把事件流聚合为「计划/感知/决策/断言/顾问/页面/通知」思考块，驱动实时页的思考流与顶部闭环流水线状态条（阶段为采集→感知→规划→探索→验证→脚本→回放→报告；"验证"阶段对应 Profile 三轮设备验证，回放阶段同时识别 bootstrap 准入的 `hypium_replay_*` 事件）。探索期每帧截图经 `on_snapshot` 回调追加进 `trace.snapshots` 并发送 `screen_captured`/`elements_detected` 事件，实时页的设备画面与当前元素表全程跟随；探索停止原因（如 `admission_metrics_reached`）在思考流中以中文可读文案呈现并注明后续阶段。页面图使用 React Flow：任务阶段渲染 `trace.graph`，探索型运行回退渲染 `discovery.pages/transitions` 构建的页面状态图。顾问对话页实时累积 `discovery_progress(stage=advisor_turn)` 事件携带的逐轮输入/输出留痕（探索进行中即可见），并与探索结束后 `/discovery` 返回的 `advisor_log` 按轮次合并去重；旧运行回退展示 `advisor_verdicts` 逐页结论。报告使用 iframe，脚本页可通过 API 触发 1/3 次回放；历史运行列表来自 `GET /api/runs`。

前端测试使用 vitest + Testing Library（`cd web && npm run test`），覆盖思考流聚合、Markdown 渲染（含 XSS 防护）、深链解析、流水线推导与组件冒烟。前端不依赖运行时外部字体 CDN，便于受限网络和离线开发。

## 14. 真实验收

2026-09-09：

- `run-20260909T140205Z-e9ada52e`
- `model_mock=false`
- 18 Actions，19 Snapshots，2 passed Assertions
- 19 Graph nodes，17 edges
- 3 次 Hypium 回放均 returncode 0
- SSE 事件 ID 1—117 有序
- Web live/graph/script/report 与 375/1440 视口通过
- 浏览器按钮触发 3 次回放全部成功
- 33 自动测试通过，1 个 live 测试默认跳过

## 15. 尚未完成或刻意不做

- DevEco Testing 测试工程模式没有有效模板，保持 `not_validated`。
- 登录、支付、验证码、删除和授权不在当前范围。
- 页面语义聚类仍可优化以减少动态页面过度分裂。
- OCR/OmniParser 是可插拔增强项，不是主链路硬依赖。
- API 没有生产级鉴权、队列或多租户隔离，仅用于本机开发验收。


## 自动目标发现与 Profile 生命周期

`targets/catalog.py` 封装 `bm dump -a/-n/-l`，`targets/resolver.py` 执行 bundle 精确匹配、应用名唯一匹配和多候选停止。`discovery/explorer.py` 在确定性安全策略和 20/8/900 边界内构建页面图；`discovery/advisor.py` 提供连续会话式 LLM 视觉探索顾问；`discovery/stability.py` 从三轮独立启动证据筛选稳定定位器和应用级断言；`profiles/registry.py` 通过临时文件、Schema 校验和原子替换维护 draft、candidate、verified、history 和 locked 状态。

每个 `RunTrace` 冻结 `target_query`、`resolved_target`、`profile_snapshot`、探索策略和验证结果。Hypium 生成器只读取该快照，生成脚本必须含可观测的应用 UI 断言。通过三次 Driver 回放后 Registry 才将 candidate 原子晋级为 verified。CLI、API 和 Web 使用相同的 resolve、discover、verify、generate、replay、promote、test 状态机。


### 探索治理与 LLM 视觉顾问

探索器以**逻辑页身份**（`page_path + 前台 + 全量折叠 key + 可交互结构`的 SHA-256）替代整树签名做队列去重与页数预算，信息流/热搜"同页不同内容"的抖动收敛为同一逻辑页；整树签名仍随快照保存作为证据。候选生成时，key/id 内嵌长数字 ID 或标题超长的内容型 click 降级到 input/swipe 之后；此类动作触达的页面不入队，跃迁标记 `replayable=false`。每页动作预算按类型配额（click ≤ max-2、input 1、swipe 1），保证点击富集页面也能覆盖输入与滑动。

候选动作之间的恢复按"落地页身份校验 → 一次 back → 冷启动重放"逐级兜底；`input` 动作因软键盘污染状态强制冷恢复；队列出队时仍冷启动重放路径以验证可回放性。空路径（启动首页）不重放。

`ExplorationAdvisor`（`discovery/advisor.py`）在单次探索内维护一段连续对话：稳定 system prompt + 逐页追加截图与编号候选，模型返回 `{page_summary, recommended, avoid, reason}`，对确定性候选列表重排/过滤（`avoid` 仅剔除内容型 click，input/swipe 不受影响）；同一逻辑身份复用既有建议。历史超过 `advisor_history_turns` 轮保留 system 头裁掉最旧交换，被裁页面的摘要由 payload 中的确定性上下文摘要携带。安全分类器在顾问之后执行，模型无法放行被拦截动作；任何调用失败回退启发式排序并在 `advisor` 事件中留痕（`source: model/reuse/heuristic-fallback`）。


### Profile 发现状态机

顶层 Run 使用 `bootstrap` 与 `task` 两个阶段。`bootstrap` 完成目标消歧、启动交叉校验、有界探索、三轮设备验证、candidate 生成、三次 Hypium Driver 回放和原子晋级；随后将冻结的 verified Profile 交给 `task` 阶段。多候选 Run 保持 `waiting_target_selection`，由 API/Web 继续；受限临时测试标记 `provisional`，禁止生成或执行正式回归用例。停止请求会唤醒候选等待，并在探索、验证和回放边界终止。

验证与回放过程逐轮逐次推送事件（`profile_verification_started`、`profile_verification_round_started/finished`、`hypium_replay_started/finished`），编排器不再攒批发送，实时视图在数分钟的验证/回放期间持续有反馈。`discovery_finished` 事件的 `stop_reason` 是探索内环的早停原因（如 `admission_metrics_reached` 探索提前达标），不代表整个 Run 结束。

无 verified Profile 且探索关闭、或探索/验证失败（候选保存前的阶段）时，运行降级为**实时模式**（`trace.live_mode` + `profile_status_at_start=absent`）：跳过 Profile 引导，用 `LaunchSpec` + 无定位器辅助的 `ToolExecutor` 和 `PlanningContext.from_resolved` 直接执行任务，强制关闭脚本生成/回放；候选已保存后的 Hypium 回放门控失败与晋级失败仍按原语义失败。`bootstrap_only` 运行不降级，失败即失败。

启动时若命中 verified Profile，先做**快速复验**：`core_flows.pages` 与验证定位器使用同一哈希空间（结构身份 `structural_identity`）建立页面→定位器映射并逐页回放路径；旧格式 Profile（发现期整树签名）映射失败时回退入口页强定位器检查。复验失败**不再自动失效**——保留既有 verified 文件，仅在 provenance 记录 `quick_verification` 失败证据并转入完整探索重新验证，新探索晋级时原子覆盖旧文件；`registry.invalidate` 保留为控制台手动操作。

## 16. 2026-09-17 资产流水线重构

### 16.1 架构收敛

系统收敛为「DC 会话面 + Asset Pipeline 资产流水线」两层：

- **DC 会话面**是唯一交互入口：26 个 DC 工具（含 `assert_visible` / `assert_not_visible` / `assert_text` 3 个断言工具）、push-based SSE、可恢复会话、脚本生成与 Profile 蒸馏。用户在同一段会话里完成探索、复现、断言录制与脚本产出。
- **Asset Pipeline 资产流水线**是唯一 Profile 晋级通道：1 轮设备验证 + 1 次 Hypium 回放门禁（主流程内联），剩余 2 次由 `POST /api/profiles/{id}/replay` 异步追加。锚定 Profile 生命周期与资产沉淀，不再依赖 CLI 侧的探索子命令。

两条链路共用 `profiles/registry.py` 的 draft → candidate → verified 状态机与准入门禁；准入门槛本身不变（3 个稳定定位器、3 个页面、2 个应用级断言、核心流 ≥3 页、`min_interaction_kinds` 类交互）。

### 16.2 精简项

| 精简项 | 重构前 | 重构后 | 回滚方式 |
| --- | --- | --- | --- |
| RunMode 枚举 | `regression` / `exploration` / `stability` / `reproduction` 四值可用 | 只有 `regression` 是有效值；其余三值仅用于反序列化历史 `trace.json`，解析时静默降级为 `regression`（`models.py` 的 `field_validator(mode="before")`） | 无（历史读取兼容内建） |
| Profile 验证轮次 | 3 轮独立启动验证 | 1 轮（`Settings.profile_verification_rounds`，`ProfileVerifier(rounds=N)` / `StabilityAnalyzer(required_rounds=N)` 参数化） | `PROFILE_VERIFICATION_ROUNDS=3` |
| Hypium 回放次数 | 3 次全部内联执行 | 1 次内联（`Settings.hypium_replay_attempts`）+ 2 次异步追加 | `HYPIUM_REPLAY_ATTEMPTS=3` |
| CLI `discover` 子命令 | `python main.py discover ...` 可用 | 子命令删除，调用直接报错 | 无（`bootstrap_only` 字段与 `/api/profiles/{id}/verify` 端点保留） |
| 前端运行模式下拉 | 启动器提供运行模式下拉，CLI `--mode` 接受四值 | 下拉删除，改为固定徽章「回归测试」；CLI `--mode` 只剩 `{regression}` | 无 |
| 前端 Tab | 8 个 Tab | 5 个 Tab（会话 / 页面关系图 / 脚本与回放 / Profile 资产 / 历史运行） | 旧深链 tab 值兼容映射（live / advisor / dc → session，report → script），见 `web/src/app/deep-links.ts` |

### 16.3 保留项

- `bootstrap_only` 字段与 orchestrator 的 3 处分支保留，语义不变。
- `discovery/` 模块（explorer / advisor / stability / verification）保留：不再通过 CLI 暴露，只被 `dc/distill.py`、`agents/orchestrator.py` 和 `POST /api/profiles/{id}/verify` 消费。
- Live Mode 的四个前端组件保留，作为「会话」Tab 内的「资产流水线降级」子视图。
- `profile_verification_started` / `profile_verification_round_started` / `profile_verification_round_finished` 事件保留，验证阶段仍逐轮推送。

### 16.4 比赛硬性要求映射

| 命题要求 | 新架构承载方式 |
| --- | --- |
| 脚本含明确步骤 / 断言 / 检查点 | DC 断言工具（`assert_visible` / `assert_not_visible` / `assert_text`，渲染为 Hypium `driver.check_component_exist(...)`）+ Hypium 检查点渲染 + Live 兜底断言 |
| 至少 2 类测试场景 | 资产流水线（回归）+ DC 会话历史（探索 / 问题复现）+ 脚本目录（多场景沉淀）+ 手动追加回放（稳定性） |
| 至少 3 个页面 / 核心流程 | registry `_validate_admission_assets` 保留 `candidate_pages >= 3` 与核心流 `pages >= 3`；DC 蒸馏前校验 `len(page_paths) >= 3` |
| 用例沉淀与复用 | Profile 生命周期 draft / candidate / verified / history / locked + DC 蒸馏新路径 |
| 页面状态图 | `graph/page_graph.py` + 前端「页面关系图」Tab |
| 3 次连续成功 | 主流程内联 1 次 + `POST /api/profiles/{id}/replay` 异步追加 2 次 |

### 16.5 新增端点与事件

- `POST /api/dc/sessions/{session_id}/profile/distill`：请求体 `{"bundle_name": ..., "main_ability": ...}`，响应 `DcDistillResult{profile_id, status, pages_covered, stable_locators, assertions, replay_run_id, replay_passed, warnings}`。前置条件：会话覆盖 ≥3 个不同 `page_path`，且 bundle / ability 非占位值（否则 422）；无录制操作 → 409。流程：纯 CPU 提取（<1s）→ 1 轮设备验证 → 1 次 Hypium 回放 → promote。前端 DC 面板提供「蒸馏为 Profile」按钮。
- `POST /api/profiles/{profile_id}/replay`：请求体 `{"attempts": 2}`（1—3 的整数），异步追加回放证据。candidate 累计满门禁次数且全部通过后自动晋级 verified；已 verified 的 Profile 只追加审计证据、状态与门禁资产不变；锁定 / draft / invalid 一律拒绝（409 / 422）。Profile 卡片显示「已记录回放数/3」进度条。
- DC 蒸馏 SSE 事件：`profile_distill_started` / `profile_distill_finished` / `profile_distill_failed`。
- 26 个 DC 工具（原 23 个 + 3 个断言工具）由 `dc/tools.py` 定义，`dc/generator.py` 负责 Hypium 渲染。`DcScriptArtifact.replay_eligible` 的语义是**可执行（runnable）**：只取决于「有可回放动作」与「应用身份非占位 bundle」，`purpose` 相应为 `acceptance` / `dc_recording`；缺显式断言只把 `confidence` 降为 `medium`（见 `cases/builder.py` 的 `evaluate_runnable` / `evaluate_confidence`）。
