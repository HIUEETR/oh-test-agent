# OpenHarmony 多模态测试 Agent 架构

更新日期：2026-09-09

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

## 6. Planning 与真实模型兼容

### 6.1 结构化输出

- Planning 和 Vision 使用 Pydantic AI 的 Pydantic 输出模型。
- Tool Decision 使用 `PromptedOutput(ToolDecision)`，降低不同 OpenAI-compatible 端点对原生 tool choice 的兼容差异。
- `AGENT_DISABLE_THINKING=true` 时增加端点兼容参数，解决 thinking 与结构化工具输出冲突。
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
- 拒绝登录、支付、验证码、删除、卸载、清除和授权；
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

SSE 从 SQLite 按事件 ID 增量读取，终止状态且没有新事件后关闭。React 控制台使用 EventSource 展示事件，同时轮询 Run Trace；页面图使用 React Flow，报告使用 iframe，脚本页可通过 API 触发 3 次回放。

前端不依赖运行时外部字体 CDN，便于受限网络和离线开发。

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
