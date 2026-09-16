# OpenHarmony 多模态智能 UI 测试用例生成系统

> 文档类型：项目任务书、实施计划与验收标准
> 计划开始日期：2026 年 9 月 9 日
> 计划提交日期：2026 年 9 月 23 日
> 当前阶段：任务书已完成；项目实施尚未开始
> 主要运行环境：DevEco 本地模拟器，后续可扩展至真实 HarmonyOS 设备
> 文档目标：将命题要求、调研结论、技术方案、开发任务、测试标准和交付材料统一为一份可直接执行的工程任务书。

---

## 1. 文档使用说明

本文件包含三类内容，必须区别对待：

### 1.1 命题方的硬性要求

以下内容来自原始命题说明，是本项目必须覆盖的验收要求：

1. 支持采集或输入 OpenHarmony 应用页面截图，并结合布局信息或控件属性识别主要可交互元素。
2. 至少支持 3 类常见 UI 控件或交互的识别与测试脚本生成，例如按钮点击、文本输入、列表滑动、页面返回、Tab 切换、弹窗确认等。
3. 能够生成可复用的 UI 自动化测试用例脚本，脚本具备明确步骤、断言或检查点，并可以在指定测试框架中执行或半自动执行。
4. 至少支持 2 类测试场景，例如问题复现、探索性测试、稳定性压测、核心流程回归测试等。
5. 至少基于一个 OpenHarmony 示例应用完成验证，覆盖不少于 3 个页面或核心流程。
6. 提交系统架构设计文档、测试用例生成说明、脚本执行结果、验证报告和演示材料。

### 1.2 命题方鼓励的挑战目标

以下内容属于挑战目标，应在 P0 主线稳定后实现：

1. 使用多模态模型理解截图和布局结构，识别页面意图、核心操作入口和潜在测试路径。
2. 根据用户自然语言描述生成问题复现脚本。
3. 自动探索页面跳转关系，生成页面状态图或核心流程图。
4. 生成压力测试脚本，例如重复点击、连续滑动、页面反复进入退出和长时间运行测试。
5. 分析执行结果，识别崩溃、卡死、白屏、页面无响应和布局异常。
6. 将探索过程沉淀为可维护、可复用的回归测试用例。
7. 形成可进一步孵化为 OpenHarmony 智能测试 Agent 或应用质量保障平台的架构基础。

### 1.3 当前团队的实现设想

以下内容是当前设计建议，可以根据调研和实现风险调整，不得凌驾于命题硬性要求之上：

- 使用 Python 编写后端，并提供 Web 前端。
- 前端流式展示 UI 自动化测试用例生成过程、执行日志和页面关系。
- 使用一个可切换的 OpenAI-compatible 云端模型接口。
- 参考浏览器 UI Agent 和 PageEyes 的感知、规划、工具调用思路。
- 使用 Pydantic AI，不绑定目前无法唯一确认的 `agentharness`。
- 使用 `uv` 管理 Python 环境，使用 `ruff` 进行代码检查。
- 不预先绑定任何第三方应用；先通过目标应用准入门禁选定一个可安装、可重置、可离线验证的 OpenHarmony 应用。
- 实现偏差（2026-09-12）：面向信息流类应用的探索治理改造后，Profile 准入的交互类型门槛由固定 3 类改为可配置 `min_interaction_kinds`（默认 2、上限 3），其余 5.3 准入条件不变；同时支持无 verified Profile 的实时模式执行（不产出正式回归脚本），`bootstrap_only` 的 Profile 生成流程保持原验收语义。
- 实现偏差（2026-09-13）：真实设备验收暴露并修复三类问题——① 启动时 verified Profile 快速复验的页面映射改用结构身份（与验证定位器同哈希空间），复验失败不再自动销毁 Profile，仅记录证据并转入完整探索重新验证；② 任务描述安全门只拦永久禁用项与凭证词，"确认/提交/发送"等门控词仅在工具决策层面按 `allow_*` 开关拦截；③ Hypium 生成器把 swipe 方向规范化为大写枚举（Hypium 不接受小写），并新增 OPEN_APP 冷启动静默期与截图前层级稳定轮询，避免截到启动 logo 页。准入门禁本身不变。
- 实现偏差（2026-09-17）：资产流水线重构——① RunMode 枚举收缩为 regression 单值（exploration/stability/reproduction 仅用于历史 trace 读取兼容）；② Profile 验证从 3 轮精简为 1 轮（Settings.profile_verification_rounds 可调回 3）；③ Hypium 回放门禁从 3 次内联改为 1 次内联 + 2 次异步追加（POST /api/profiles/{id}/replay）；④ CLI discover 子命令删除，bootstrap_only 字段保留供 /api/profiles/{id}/verify 端点与 DC 蒸馏路径使用；⑤ DC Mode 新增 3 个断言工具（23→26）与「蒸馏为 Profile」能力（POST /api/dc/sessions/{id}/profile/distill），脚本 replay_eligible 改为条件判定；⑥ 前端 8 Tab 合并为 5 Tab（会话/页面图/脚本与回放/Profile 资产/历史运行）。准入门禁本身不变（3 页面、3 稳定定位器、2 断言、min_interaction_kinds 类交互）。

---

## 2. 项目目标

### 2.1 总体目标

实现一个可以在本机启动的智能 UI 测试系统，使用户能够输入类似下面的自然语言任务：

```text
打开当前目标应用，进入其核心流程，完成输入或选择操作，打开稳定内容页，检查目标状态，然后返回并确认前一状态保持正确。
```

系统应自动完成：

```text
自然语言需求
  ↓
测试步骤规划
  ↓
截图和 UI 元素采集
  ↓
页面理解与目标定位
  ↓
HDC 实时操作
  ↓
截图、断言和状态记录
  ↓
页面关系图更新
  ↓
Hypium Python 用例生成
  ↓
Hypium 用例执行
  ↓
测试报告与用例沉淀
```

### 2.2 截止日前的核心成果

截止 2026 年 9 月 23 日，必须保证以下成果可以现场演示：

1. DevEco 本地模拟器能够运行已通过准入的目标应用。
2. 后端可以通过 HDC 连接模拟器并采集截图。
3. 系统可以识别文本、按钮、输入框、列表/滚动区域等元素。
4. 系统可以执行点击、文本输入、滑动、返回和断言。
5. 系统可以把一次自然语言任务拆解为原子操作并执行。
6. 系统可以将至少 3 个页面记录为页面节点，并生成带动作标签的跳转边。
7. 系统可以生成 Hypium Python 测试脚本。
8. 生成脚本在目标应用上至少连续执行成功 3 次。
9. Web 前端可以实时显示任务进度、截图、日志、脚本和页面图。
10. 能够生成一份包含步骤、截图、断言、耗时和失败原因的报告。
11. 目标应用、其 Bundle Name、启动 Ability、重置方式和测试数据策略均已记录在目标应用档案中。

---

## 3. 调研结论

### 3.1 官方 Hypium / DevEco Testing

官方 Hypium Python 指南是测试脚本格式、工程结构和运行方式的最高优先级参考。项目不能自行发明一种类似 Hypium 的 Python 文件后就声称完成了 Hypium 集成；必须由本机 Hypium Runner 执行生成的测试工程并保留其报告。

调研结论：

- Hypium 有两条必须区分的使用路径：Driver 模式用于单设备、单任务的交互调试；测试工程模式用于组织、执行和报告正式回归用例。
- 实时 Agent 可用 HDC 完成探索和截图采集，但正式“生成脚本已验证”的结论只能来自 Hypium 测试工程的实际运行结果。
- 生成器必须输出 Hypium 测试工程内的 Python 用例及其 JSON 配置，而非只输出一段孤立 Python 代码。
- 用例必须拥有 `setup`、`process`、`teardown` 三段生命周期：前置状态与应用启动、原子操作和断言、失败证据保留及清理。
- Hypium 用例应使用 Driver 的组件定位、组件操作、等待、返回和断言能力；测试工程由 `python -m hypium run` 的本机可用参数运行。
- 测试执行服务必须捕获 Runner 的退出码、标准输出、标准错误、报告目录和失败截图，并回传给 Web 前端。
- 控件定位应优先使用稳定的文本、key、id、type 或可交互属性；当只能使用坐标时，必须在脚本、Web 页面和报告中标记为“坐标降级定位”。
- 当前环境的实际 Hypium 版本、导入路径、JSON 字段、运行参数和报告目录结构必须由 smoke test 锁定；生成器不得猜测不同版本的 API。

官方参考：

- Hypium Python 指南：`https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/hypium-python-guidelines`
- DevEco Testing 相关官方文档和当前安装版本说明。

### 3.1.1 Hypium 在本项目中的实际测试链路

```text
自然语言任务
  ↓
Agent 实时探索：HDC + 截图 + UI 感知
  ↓
Run Trace：动作、定位候选、断言、页面状态、截图
  ↓
Hypium Project Generator
  ├── testcases/<case_id>.py
  ├── testcases/<case_id>.json
  └── run configuration / report directory
  ↓
python -m hypium run <本机已验证参数>
  ↓
Hypium report + stdout/stderr + 失败证据
  ↓
Web 报告、页面图和可维护回归用例
```

测试工程的最小闭环分为四个门禁：

1. **Driver smoke test**：连接指定模拟器，启动目标应用，定位一个稳定组件，完成一次点击并断言结果。
2. **项目 smoke test**：将同一操作写为 `testcases/*.py` 与匹配 JSON 配置，通过 Hypium Runner 执行并生成报告。
3. **生成脚本验证**：由系统从真实 Run Trace 生成相同结构的用例，Runner 真实执行；不允许手工替换生成文件后仍标记为“自动生成成功”。
4. **回归稳定性验证**：重置目标应用后连续运行 3 次；每次都必须保留 Hypium 运行日志、报告路径、截图和退出码。

### 3.2 PageEyes Agent

本地参考项目：

```text
D:\Work\Code\Harmony\project\Project_Research\page-eyes-agent
```

可复用的设计和实现经验：

- 使用 Pydantic AI 进行 Agent 编排和结构化工具调用。
- 将规划阶段和执行阶段分开，先把自然语言拆解为原子步骤，再逐步执行。
- Harmony 适配层使用 HDC 连接设备，支持截图、点击、输入、滑动和打开应用。
- OmniParser 服务通过 FastAPI 接收截图，并返回元素、边界框、文本和空间关系。
- 工具调用前后记录步骤、截图、参数、耗时和成功状态。
- 执行结束后生成 HTML 报告。

当前项目不足：

- 没有本项目所需的 Hypium Python 用例生成器。
- 没有页面状态节点和跳转边的持久化模型。
- 没有本项目所需的 Web 任务控制台和页面关系图。
- 现有代码的部分行为依赖外部模型服务和存储服务，不能原样作为截止日前唯一依赖。

本项目的处理方式：**复用接口设计和稳定的 HDC 适配思路，不直接把 PageEyes 整个项目复制为本项目主体。**

### 3.3 AUITestAgent

本地参考项目：

```text
D:\Work\Code\Harmony\project\Project_Research\AUITestAgent
```

可复用的设计思想：

- 由自然语言测试需求驱动 GUI 交互。
- 将交互执行和结果验证分离。
- 通过多轮交互记录上下文和校验信息。
- 结果中同时保留操作轨迹和验证结论。

限制：

- 当前目录主要包含论文、README、评估结果和演示材料，没有可直接移植的完整业务实现。
- 其 Android/商业应用实验不能直接证明 OpenHarmony/Hypium 兼容性。
- 不能把论文中的实验结果当作本项目的实测结果。

### 3.4 总体技术结论

最终选择：

| 方向        | 决策                                 |
| ----------- | ------------------------------------ |
| 后端语言    | Python 3.12+                         |
| 后端框架    | FastAPI + asyncio                    |
| Agent 框架  | Pydantic AI                          |
| 模型        | 可切换 OpenAI-compatible VLM/LLM     |
| 设备控制    | HDC                                  |
| 感知        | UI 层级信息优先，OCR/OmniParser 辅助 |
| 测试脚本    | Hypium Python                        |
| 前端        | React + Vite + TypeScript            |
| 页面图      | React Flow 或同类节点图方案          |
| 实时通信    | SSE                                  |
| 数据存储    | SQLite + 本地 JSON/截图/日志文件     |
| Python 管理 | uv                                   |
| 质量检查    | ruff + pytest                        |
| 部署        | 本机一键启动                         |

---

## 4. 产品范围

### 4.1 P0 必须实现的功能

#### A. 环境与设备

- Python、uv、ruff、Node.js、npm 版本检查。
- 模型 API 配置检查。
- HDC 设备发现和连接检查。
- Hypium Driver smoke test、测试工程 smoke test 和 Runner 报告检查。
- 目标应用档案加载、安装、启动和重置检查。
- 设备断开时给出明确错误，不继续执行危险动作。

#### B. 页面采集与元素识别

- 采集当前屏幕截图。
- 保存截图和采集时间。
- 读取 UI 层级或控件属性。
- 通过 OCR 识别可见文本。
- 通过 OmniParser 或其他视觉解析器补充图标和边界框。
- 为元素生成统一 ID。
- 保存元素文本、类型、bbox、可点击、可编辑、可滚动和数据来源。
- 显示可视化标注截图。

#### C. Agent 规划与执行

- 输入自然语言测试需求。
- 将需求拆解为顺序执行的原子步骤。
- 只能调用一个设备操作工具，禁止并行操作设备。
- 每个动作执行前重新获取当前页面状态。
- 目标元素不存在时有限重试。
- 操作失败时立即记录失败并停止后续步骤。
- 支持等待、关键词等待和操作后截图。
- 支持手动停止任务。

#### D. UI 交互

至少稳定实现：

1. 按钮点击；
2. 文本输入；
3. 列表或页面滑动；
4. 页面返回；
5. Tab/频道切换；
6. 弹窗确认或关闭；
7. 页面包含文本断言；
8. 页面不包含文本断言。

#### E. Hypium 脚本生成

- 根据执行轨迹生成一个可运行的 Hypium 测试工程片段：`testcases/<case_id>.py`、`testcases/<case_id>.json` 和运行元数据。
- 生成明确的用例名称、测试目的、`setup`、`process`、`teardown`、步骤说明和断言。
- 为每个动作生成对应定位、操作、等待和失败证据采集；定位优先级为 key/id、精确文本、type + text、属性/空间关系、坐标。
- 在 JSON 配置中生成或引用目标设备、目标应用、报告目录和超时设置；具体字段由本机 smoke test 固化。
- 调用封装后的 Hypium Runner 执行生成用例，捕获退出码、stdout、stderr、报告目录和失败截图。
- 生成脚本前后进行 Python 语法检查；只有 Runner 退出成功且断言通过，才能标记为“Hypium 验证通过”。
- 坐标定位必须输出告警，并以 `stability=degraded` 记录到用例元数据。

#### F. 页面关系图

- 保存页面截图。
- 生成页面稳定签名。
- 对相同页面进行去重。
- 创建动作前后的页面节点。
- 保存从页面 A 到页面 B 的动作名称和目标元素。
- 页面图中展示节点截图、页面名称和边标签。
- 支持点击节点查看元素和执行记录。

#### G. Web 前端

- 任务创建页。
- 设备和模型健康状态。
- 任务执行状态。
- SSE 实时日志。
- 当前截图。
- 当前识别到的元素。
- 脚本流式生成或分段显示。
- 页面关系图。
- 测试报告查看和下载。
- 任务失败、停止和重新运行。

### 4.2 P1 应实现的功能

- 第二个符合准入条件的目标应用适配；不预先指定第三方应用名称。
- 探索性测试模式。
- 已有测试序列的 N 次重复执行。
- 执行过程中的崩溃、超时、白屏、无变化检测。
- 自然语言问题复现模式。
- 成功轨迹保存为回归用例。
- 用例重新加载和执行。
- 运行结果摘要。
- 生成 Markdown、JSON 或 HTML 报告。
- 模型供应商切换。

### 4.3 P2 不作为截止日前主线

- 完整 Web、Android、iOS、桌面端跨平台矩阵。
- 模型训练或专用微调。
- 多用户权限、账号体系和远程任务队列。
- Docker 化生产部署。
- 复杂业务的自动登录、支付、验证码和下单。
- 工业级全自动缺陷判定。
- 无限制页面自动探索。

---

## 5. 目标应用准入与演示对象

### 5.1 不预先绑定具体应用

截止日前不预先绑定任何指定第三方应用，也不承诺特定第三方应用兼容性；系统必须以“目标应用档案”驱动，而不是把 Bundle Name、页面文本或页面结构硬编码到后端。

当前工作区中的 `ArkDO` 与 `MyApplication` 仅是候选工程，不自动成为被测对象：

- `ArkDO` 页面较多，但核心功能涉及外部服务和网络状态，必须先证明可稳定启动、重置和离线/可控数据运行。
- `MyApplication` 当前为单页模板，未达到“三个页面或核心流程”的命题验收条件；只有扩展出稳定流程后才可作为目标应用。

### 5.2 目标应用档案

首次真实运行前，必须提交一个不含敏感凭据的 `TargetAppProfile`，至少包含：

```text
target_app_id
display_name
bundle_name
main_ability
install_artifact_or_project_path
device_selector
launch_strategy
reset_strategy
test_data_strategy
permission_and_popup_strategy
stable_locator_inventory
core_flow_name
core_flow_pages
known_limitations
```

其中 `reset_strategy`、`test_data_strategy` 和 `stable_locator_inventory` 为空时，目标应用不得进入正式 Hypium 回归验证。

### 5.3 准入标准

目标应用必须同时满足以下条件：

1. 能在 DevEco 模拟器安装、启动并被 Hypium 与 HDC 发现。
2. 不依赖登录、验证码、支付、真实个人信息或不可控网络内容。
3. 可以在不重装应用的情况下恢复到测试初始状态，或有记录明确的重置步骤。
4. 至少存在 3 个可达页面或核心流程，且能覆盖点击、输入、滑动、返回/Tab/弹窗中的至少 3 类交互。
5. 至少 3 个关键操作具有稳定的 key/id/text/type 定位候选。
6. 主流程至少有 2 个可自动断言的检查点。
7. 运行 3 次后，页面顺序和断言结果可重复。

### 5.4 默认演示流程

不指定业务名称，统一以“入口页 → 可输入/选择页 → 列表或内容页 → 详情或状态页 → 返回验证”的四状态流程作为基准。实际页面名、目标元素和断言文本从 `TargetAppProfile` 读取。

```text
入口页
  ↓ 入口操作
交互页
  ↓ 输入、选择或切换
内容页
  ↓ 选择一个稳定对象
详情/状态页
  ↓ 返回并验证前一状态
```

---

## 6. 系统架构

```text
┌────────────────────────────────────────────────────┐
│ React Web 前端                                     │
│ 任务配置 / 实时日志 / 截图 / 脚本 / 页面关系图 / 报告 │
└───────────────────────┬────────────────────────────┘
                        │ HTTP + SSE
┌───────────────────────▼────────────────────────────┐
│ FastAPI 后端                                        │
│ Run API / Event API / Graph API / Script API        │
└───────────────┬───────────────┬────────────────────┘
                │               │
┌───────────────▼──────┐ ┌──────▼───────────────────┐
│ Agent 编排层          │ │ 数据与报告层              │
│ PlanningAgent         │ │ SQLite                    │
│ ExecutionAgent        │ │ JSON/截图/日志/脚本       │
│ Tool Registry          │ │ Markdown/HTML 报告        │
└───────────────┬──────┘ └──────────────────────────┘
                │
┌───────────────▼────────────────────────────────────┐
│ 设备与感知层                                        │
│ HDC Adapter / Screenshot / UI Hierarchy / OCR       │
│ OmniParser Adapter / Element Normalizer              │
└───────────────┬────────────────────────────────────┘
                │
       DevEco Simulator / HarmonyOS Device
```

### 6.1 设备适配接口

后端 Agent 不得直接依赖具体 HDC 命令，统一使用设备接口：

```text
connect()
health_check()
screenshot()
collect_ui_hierarchy()
collect_logs()
click(x, y)
input_text(text)
swipe(direction, repeat_times)
back()
open_app(app_identifier)
wait(seconds)
close()
```

### 6.2 感知接口

```text
PerceptionService.analyze(snapshot) -> ScreenSnapshot
```

`ScreenSnapshot` 至少包含：

```text
snapshot_id
run_id
captured_at
image_path
width
height
page_title
source
elements[]
```

`UIElement` 至少包含：

```text
element_id
content
type
bbox
clickable
editable
scrollable
score
source
locator_candidates
```

### 6.3 Agent 状态机

```text
created
  ↓
preflight
  ↓
planning
  ↓
executing
  ↓
verifying
  ↓
graph_updating
  ↓
script_generating
  ↓
script_executing
  ↓
completed
```

失败状态：

```text
failed_device
failed_model
failed_element
failed_action
failed_assertion
failed_script
stopped_by_user
```

### 6.4 页面节点和跳转边

页面节点：

```text
PageNode
├── node_id
├── signature
├── screenshot_path
├── title
├── visible_texts
├── element_summary
└── first_seen_at
```

跳转边：

```text
TransitionEdge
├── edge_id
├── from_node
├── to_node
├── action
├── target_element
├── precondition
├── postcondition
├── confidence
├── before_screenshot
└── after_screenshot
```

去重条件：

- 关键文本集合相似；
- 可交互元素结构相似；
- 截图感知哈希相似；
- 页面标题或稳定 key/id 一致。

不得仅依据截图文件名或单一像素相似度创建页面节点。

---

## 7. 后端 API 设计

### 7.1 健康检查

```text
GET /api/health
```

返回：

```json
{
  "status": "ok",
  "model": {"configured": true, "reachable": true},
  "device": {"connected": true, "id": "..."},
  "hypium": {"importable": true, "version": "..."}
}
```

### 7.2 设备查询

```text
GET /api/devices
```

### 7.3 创建运行任务

```text
POST /api/runs
```

请求示例：

```json
{
  "target_app_id": "selected-target-app",
  "task": "完成目标应用档案定义的核心流程，并检查详情或状态页的稳定断言",
  "mode": "regression",
  "device_id": "optional",
  "max_steps": 20
}
```

`mode` 支持：

```text
regression
exploration
stability
reproduction
```

### 7.4 运行状态

```text
GET /api/runs/{run_id}
```

### 7.5 停止运行

```text
POST /api/runs/{run_id}/stop
```

### 7.6 SSE 事件流

```text
GET /api/runs/{run_id}/events
```

事件类型：

```text
run_started
preflight_passed
screen_captured
elements_detected
plan_created
action_started
action_finished
assertion_passed
assertion_failed
page_discovered
edge_created
script_generated
execution_started
execution_finished
run_failed
run_finished
```

事件格式：

```json
{
  "event_id": 12,
  "run_id": "run-001",
  "type": "page_discovered",
  "timestamp": "2026-09-08T12:00:00Z",
  "message": "发现详情页",
  "payload": {}
}
```

### 7.7 页面图、脚本和报告

```text
GET /api/runs/{run_id}/graph
GET /api/runs/{run_id}/script
GET /api/runs/{run_id}/report
POST /api/runs/{run_id}/generate
POST /api/runs/{run_id}/execute
```

---

## 8. 前端功能设计

### 8.1 任务配置页

必须支持：

- 选择已通过准入的目标应用；
- 输入自然语言测试任务；
- 选择回归、探索、稳定性或问题复现模式；
- 选择设备；
- 查看模型和 Hypium 预检状态；
- 启动任务。

### 8.2 实时执行页

必须展示：

- 当前任务状态；
- 当前阶段；
- 当前步骤；
- 当前截图；
- 当前页面元素；
- 操作日志；
- 断言结果；
- 错误原因；
- 停止任务按钮。

### 8.3 页面关系图页

必须展示：

- 页面节点截图；
- 页面名称或自动摘要；
- 节点发现顺序；
- 动作边；
- 目标元素；
- 边置信度；
- 点击节点后查看页面元素和执行记录。

### 8.4 脚本与报告页

必须展示：

- 生成的 Hypium Python 用例和 JSON 配置；
- 代码生成时间；
- 定位策略；
- 坐标降级警告；
- 每一步断言；
- 脚本执行结果；
- 截图和日志；
- 报告下载入口。

---

## 9. 脚本生成规则

### 9.1 定位优先级

```text
key/id
  > 精确文本
  > type + text
  > 可交互属性 + 空间关系
  > 坐标
```

### 9.2 生成要求

每个生成用例必须包含：

- 用例名称；
- 测试目的；
- 前置条件；
- 测试步骤；
- 控件定位；
- 操作参数；
- 等待策略；
- 断言；
- 失败截图或报告信息；
- 生成来源 run_id；
- 定位稳定性告警。

此外，生成器必须输出/更新与 Python 用例配套的 Hypium JSON 配置。配置字段以本机 smoke test 生成的基线为模板，仅替换用例 ID、设备选择、目标应用、报告目录、超时与生成元数据；不能由大模型自由生成未知字段。

Python 用例的生命周期固定为：

```text
setup
  启动或恢复目标应用，建立 Driver，记录前置截图
process
  按 Run Trace 执行操作、等待并断言
teardown
  采集失败证据，输出运行元数据，恢复或清理测试状态
```

### 9.3 坐标降级

当页面只能通过截图/OCR/视觉模型获得 bbox 时，允许生成坐标定位，但必须同时：

1. 在代码注释中标识坐标定位；
2. 在 Web 界面显示告警；
3. 在报告中写明分辨率和布局变化可能造成回放失败；
4. 对通过准入的目标应用优先补充稳定 key/id，而不是长期依赖坐标。

---

## 10. 测试场景

### 10.1 核心流程回归测试

示例：

```text
启动目标应用
执行目标应用档案中的入口操作
输入或选择固定测试数据
进入稳定内容页
选择一个固定对象
断言详情或状态页包含目标应用档案定义的检查点
点击返回
断言回到内容页且固定对象仍可见
```

### 10.2 探索性测试

从首页开始，识别当前可交互元素，选择有限数量的安全动作，记录：

- 进入过的页面；
- 已尝试的元素；
- 页面跳转关系；
- 未处理的弹窗；
- 失败动作；
- 页面截图。

探索必须有最大步数和最大节点数，禁止无限探索。

### 10.3 稳定性测试

对已成功验证的流程进行 N 次重复执行，至少统计：

- 成功次数；
- 失败次数；
- 失败步骤；
- 平均耗时；
- 最大耗时；
- 截图和日志路径。

### 10.4 问题复现测试

输入格式：

```text
复现：打开应用，完成核心流程中的输入或选择操作，进入目标状态后返回，检查前一内容状态是否仍然存在。
预期：返回后目标应用档案指定的内容状态不为空。
```

系统需要将“操作步骤”和“预期结果”分离，并生成显式断言。第一版不自动判断复杂业务因果，只验证用户指定的步骤和检查点。

---

## 11. 计划表

> 状态符号：`[ ]` 未完成，`[x]` 已完成，`[!]` 存在风险或需要降级。

| 日期       | 阶段               | 任务                                                                                 | 产出                      | 验收标准                                                    | 状态 |
| ---------- | ------------------ | ------------------------------------------------------------------------------------ | ------------------------- | ----------------------------------------------------------- | ---- |
| 9 月 9 日  | 范围与目标应用门禁 | 创建`TargetAppProfile`；审查候选工程的启动、重置、数据和控件稳定性                 | 目标应用档案与准入记录    | 选定一个目标应用，或明确记录“无合格应用”这一阻塞状态      | [ ]  |
| 9 月 9 日  | 环境预检           | 确认 Python、uv、ruff、Node.js、PyCharm、DevEco 模拟器、HDC 和 Hypium                | 环境检查表                | `hdc list targets`、Hypium 导入和设备发现均有真实输出     | [ ]  |
| 9 月 10 日 | Hypium Driver 验证 | 编写最小 Driver 用例：连接模拟器、启动目标应用、定位组件、点击并断言                 | Driver smoke test         | 一次真实点击和断言成功，记录 API 导入路径与设备参数         | [ ]  |
| 9 月 10 日 | Hypium 项目验证    | 建立`testcases/*.py + *.json` 的最小测试工程，并通过 `python -m hypium run` 执行 | Project smoke test 与报告 | Runner 退出码为 0，报告目录和 stdout/stderr 已保存          | [ ]  |
| 9 月 11 日 | 目标应用稳定化     | 固定至少 3 个页面/核心状态、测试数据、重置方式、关键控件 key/id/text 和断言          | 可重复目标流程            | 主流程可从初始状态连续运行两次                              | [ ]  |
| 9 月 12 日 | 设备与感知层       | 完成 HDC 连接、截图、日志、UI 层级、OCR/OmniParser 标准化                            | Harmony Device Adapter    | 能展示当前页面截图、元素和定位候选                          | [ ]  |
| 9 月 13 日 | Agent 规划         | 将自然语言任务转为结构化原子步骤和预期断言                                           | PlanningAgent             | 对目标应用主流程不遗漏步骤或断言                            | [ ]  |
| 9 月 14 日 | Agent 执行         | 完成单工具执行、有限重试、失败即停、等待与断言                                       | ExecutionAgent            | 能经 HDC 完成目标应用主流程                                 | [ ]  |
| 9 月 15 日 | 执行轨迹           | 记录动作、定位候选、截图、页面状态、元素、耗时和错误                                 | Run Trace                 | 一次运行可完整复盘并导出                                    | [ ]  |
| 9 月 16 日 | 页面图             | 完成页面签名、节点去重和跳转边记录                                                   | Page Graph v1             | 至少 3 节点、2 条动作边                                     | [ ]  |
| 9 月 17 日 | Hypium 生成器      | 从真实 Run Trace 生成 Python 用例、JSON 配置、运行元数据和坐标降级告警               | Generated Hypium Project  | 生成文件与本机 smoke test 工程结构一致                      | [ ]  |
| 9 月 18 日 | Hypium 回放        | Runner 执行生成用例；归集退出码、报告、截图和日志                                    | Generated Script Run      | 生成用例首次真实执行成功                                    | [ ]  |
| 9 月 19 日 | 后端 API           | 完成任务、状态、图、脚本、报告、停止和 Hypium 执行 API                               | FastAPI API v1            | API 可独立创建并查询一轮运行                                | [ ]  |
| 9 月 20 日 | SSE 与 Web 控制台  | 完成事件流、任务配置、实时日志、截图、元素和脚本视图                                 | Web Console v1            | 浏览器可发起任务并实时接收 Runner 状态                      | [ ]  |
| 9 月 21 日 | 页面图与结果页     | 完成关系图、节点详情、Hypium 报告和失败证据查看                                      | Web Graph/Report          | 可从一轮运行定位任意步骤的截图、脚本和报告                  | [ ]  |
| 9 月 22 日 | 回归与增强测试     | 连续运行生成用例 3 次；加入超时、无变化和 Runner 失败分类                            | 验证报告与失败分类        | 三次结果、报告路径和失败处理均有记录                        | [ ]  |
| 9 月 22 日 | 材料整理           | 完成架构图、Hypium 用例生成说明、验证报告、演示稿和录屏                              | Submission Materials      | 材料只陈述真实验证结果                                      | [ ]  |
| 9 月 23 日 | 冻结交付           | 全链路彩排、修复阻塞问题、整理启动命令和提交文件                                     | 最终提交包                | 按 README 能完成“探索 → 生成 → Hypium 回放 → 报告”闭环 | [ ]  |

### 11.1 阶段优先级

如果进度落后，必须按以下顺序削减：

```text
保留：已准入目标应用闭环
保留：HDC 采集与操作
保留：Hypium 生成与执行
保留：页面图
保留：Web 基础展示
降级：第二目标应用适配
降级：压力测试
降级：复杂异常识别
删除：跨端扩展、登录、支付、多用户和模型训练
```

---

## 12. 环境预检清单

### 12.1 Python 环境

```powershell
python --version
uv --version
uv run python --version
uv run ruff --version
uv run pytest --version
```

### 12.2 Hypium 环境

```powershell
python -c "import hypium; print(hypium)"
python -m hypium --help
python -m hypium run --help
```

必须完成以下验证，不得只确认模块能够导入：

1. **Driver smoke test**：使用本机已安装版本的 `Driver` 创建驱动、连接目标设备、启动目标应用、定位一个稳定组件、点击并断言。
2. **测试工程 smoke test**：创建 `testcases/<name>.py` 与匹配 JSON 配置，执行 `python -m hypium run` 的本机有效参数，确认 Runner 产生报告。
3. **运行参数基线**：将 `python -m hypium run --help`、实际执行命令、设备选择参数、报告目录和 JSON 模板保存为 `hypium-baseline.md`，供生成器读取。
4. **失败证据基线**：刻意让一个断言失败，确认退出码非零，并且 stdout、stderr、报告和截图可被后端采集。

必须记录：

- 当前 Hypium 版本和安装路径；
- Driver 的实际导入路径、构造参数和设备连接方式；
- 组件定位、点击、输入、滑动、返回、等待和断言的实际 API；
- `testcases` 目录、Python 文件与 JSON 配置之间的对应关系；
- `setup`、`process`、`teardown` 的实际签名和运行顺序；
- Runner 的实际启动命令、报告目录和返回码语义；
- 当前模拟器是否能被 Hypium 发现。

### 12.3 HDC 环境

```powershell
hdc list targets
```

必须确认：

- 模拟器处于可连接状态；
- 可以执行截图；
- 可以执行点击；
- 可以获取设备分辨率；
- 可以启动目标应用；
- 设备断开后能够被系统检测到。

### 12.4 模型环境

使用 `.env` 或本地未提交配置：

```text
OPENAI_BASE_URL=...
OPENAI_API_KEY=...
AGENT_MODEL=...
AGENT_VISION_MODEL=...
```

要求：

- API Key 不能写入源码；
- `.env` 不提交；
- 后端启动时只显示“已配置/未配置”，不能打印完整 Key；
- 模型不可用时返回明确错误；
- 核心目标应用任务应保留可测试的固定规划数据或 Mock 模式。

---

## 13. 测试计划

### 13.1 Hypium 正式测试

Hypium 是生成用例的正式执行器，必须覆盖以下三层：

| 层级                     | 输入                            | 执行方式                 | 必须保存的证据                                  | 通过条件                     |
| ------------------------ | ------------------------------- | ------------------------ | ----------------------------------------------- | ---------------------------- |
| Driver smoke test        | 手写最小用例                    | Driver 直连模拟器        | 代码、设备 ID、截图、断言结果                   | 能完成定位、动作和断言       |
| Project smoke test       | 手写`testcases/*.py + *.json` | `python -m hypium run` | Runner 命令、退出码、stdout、stderr、报告目录   | Runner 成功并生成报告        |
| Generated replay test    | 系统生成的工程文件              | 同一 Runner 封装         | Run Trace、生成文件哈希、退出码、报告、失败截图 | 生成文件无需人工改动即通过   |
| Generated stability test | 同一生成工程                    | 重置后连续运行 3 次      | 三轮独立报告和运行摘要                          | 三次断言均通过，且无状态污染 |

每个生成用例必须执行两次检查：

1. 生成前的结构检查：Python 可解析；JSON 与已锁定的 Hypium 基线结构兼容；引用的目标应用档案存在。
2. Runner 后的结果检查：退出码为成功值；报告存在；所有关键断言通过；运行期间生成的截图和日志可从 Web 界面定位。

### 13.2 单元测试

必须覆盖：

- 页面签名生成；
- 页面节点去重；
- 跳转边创建；
- 元素定位优先级；
- bbox 边界检查；
- 坐标归一化；
- 自然语言步骤解析；
- Hypium 模板渲染；
- SSE 事件序列化；
- 运行状态转换；
- 错误报告序列化。

### 13.3 集成测试

必须覆盖：

1. 截图到元素识别；
2. 元素识别到点击；
3. 输入框识别到文本输入；
4. 滑动到关键词出现；
5. 操作前后截图到页面节点；
6. 页面节点到跳转边；
7. 执行轨迹到 Hypium 脚本；
8. Hypium 测试工程到真实 Runner 执行、报告和失败证据；
9. 后端事件到前端实时展示。

### 13.4 端到端测试

完整流程：

```text
启动后端
  ↓
启动前端
  ↓
检查设备和模型
  ↓
选择已准入目标应用
  ↓
输入自然语言需求
  ↓
规划并执行目标应用档案定义的核心流程
  ↓
显示截图和日志
  ↓
生成页面图
  ↓
生成 Hypium 脚本
  ↓
运行生成脚本
  ↓
显示报告
```

### 13.5 稳定性测试

目标应用主流程至少连续执行 3 次，并检查：

- 页面是否从初始状态开始；
- 步骤是否顺序一致；
- 页面节点是否重复膨胀；
- 失败后是否停止后续操作；
- 每次是否生成独立报告；
- 前端是否能正确显示所有运行结果。

---

## 14. 失败处理与降级策略

| 风险                  | 处理策略                                                                 |
| --------------------- | ------------------------------------------------------------------------ |
| DevEco 模拟器无法连接 | 暂停功能开发，先完成设备门禁；不能把未连接状态宣称为通过                 |
| 无应用通过准入        | 记录为项目阻塞；只完成脱离设备的模块测试，不宣称端到端验证               |
| Hypium 版本不匹配     | 以 Driver 与测试工程 smoke test 为模板源，修改生成器而不是猜测 API       |
| Hypium Runner 失败    | 保存命令、退出码、stdout、stderr、报告和截图；区分环境、生成器和断言失败 |
| 模型 API 不可用       | 使用 Mock 规划和固定示例数据测试设备、页面图和脚本流水线                 |
| UI 层级信息不可用     | 使用 OCR/OmniParser；对坐标定位输出告警                                  |
| OCR 结果不稳定        | 目标应用准入要求稳定 key/id/text；未满足时只允许降级演示                 |
| 页面加载缓慢          | 使用有限等待和关键词等待，禁止无限重试                                   |
| 弹窗遮挡              | 优先识别并关闭；无法处理则记录阻塞截图和失败原因                         |
| HDC 断开              | 立刻停止操作，保存现场，状态标记为`failed_device`                      |
| 页面未发生变化        | 检查动作结果、弹窗和加载状态，达到上限后失败                             |
| 前端开发延期          | 保留 API、HTML/JSON 报告和命令行演示作为备用                             |
| 一人开发进度不足      | 删除跨端、登录、训练、多用户和复杂异常分析                               |

---

## 15. 最终交付物

### 15.1 软件交付

- Python/FastAPI 后端；
- React/Vite/TypeScript 前端；
- 已通过准入的 OpenHarmony/HarmonyOS 目标应用及其 `TargetAppProfile`；
- HDC 设备适配器；
- UI 感知和元素标准化模块；
- Pydantic AI Agent；
- 页面图模块；
- Hypium 测试工程生成器；
- Hypium 基线工程、Driver smoke test、项目 smoke test 与生成用例回放证据；
- 运行报告生成器；
- 测试用例和 Mock 数据；
- `uv.lock`、前端锁文件和 Ruff 配置；
- 本机一键启动说明。

### 15.2 文档交付

- 项目任务书；
- 系统架构设计文档；
- 测试用例生成说明；
- Hypium 运行说明；
- 环境安装说明；
- API 说明；
- 验证报告；
- 已知问题和限制；
- 演示脚本；
- 后续产业化规划。

### 15.3 演示材料

- 完整流程录屏；
- 目标应用页面关系图；
- 生成的 Hypium 测试工程文件（Python + JSON）；
- 执行成功报告；
- 目标应用准入记录、Hypium Runner 命令和报告目录说明；
- 系统架构图和数据流图。

---

## 16. 最终验收标准

只有同时满足以下条件，才能标记项目完成：

- [ ] 至少一个目标应用通过准入，并具有完整 `TargetAppProfile`；
- [ ] 目标应用能够安装、启动和重置；
- [ ] DevEco 模拟器能够被 HDC 发现；
- [ ] Hypium Driver smoke test 能够执行；
- [ ] Hypium 项目 smoke test 能够执行并生成报告；
- [ ] 至少识别 3 类 UI 控件或交互；
- [ ] 至少支持点击、输入、滑动、返回和断言；
- [ ] 自然语言需求可以拆解为顺序步骤；
- [ ] 目标应用覆盖至少 3 个页面或核心流程；
- [ ] 页面图至少包含 3 个页面节点和 2 条跳转边；
- [ ] 测试脚本包含明确步骤和断言；
- [ ] 生成的 Hypium 测试工程能够由 Runner 实际执行；
- [ ] Runner 的退出码、报告、stdout、stderr 和失败截图均能被保存和查看；
- [ ] 目标应用主流程连续执行成功至少 3 次；
- [ ] 至少完成回归测试和探索性测试两类场景；
- [ ] 前端可以实时展示运行过程；
- [ ] 报告包含截图、日志、步骤和结果；
- [ ] 失败时不会继续执行后续危险步骤；
- [ ] 架构文档、生成说明、验证报告和演示材料齐全。

---

## 17. 当前明确的非目标

为保证截止日前完成稳定演示，本项目不承诺：

1. 对所有 OpenHarmony 应用自动生成高质量脚本；
2. 对第三方应用的登录、支付和验证码流程自动化；
3. 对任意页面进行无限制自主探索；
4. 通过视觉模型单独保证百分之百的坐标准确率；
5. 训练新的多模态模型；
6. 在截止日前形成生产级云平台；
7. 把浏览器、Android、iOS 和 HarmonyOS 同时做成同等成熟度；
8. 将论文或参考项目的实验数据直接作为本项目数据。

---

## 18. 完成定义

本项目的“完成”不是“代码能够启动”，而是以下闭环能够被第三方按照 README 和演示脚本重复执行：

```text
输入自然语言任务
  → 连接模拟器
  → 读取 TargetAppProfile
  → 采集截图和元素
  → 规划操作
  → 执行至少三类交互
  → 生成三页以上页面图
  → 生成 Hypium 测试工程（Python + JSON）
  → 由 Hypium Runner 实际执行生成工程
  → 输出可读报告
```

所有演示结论必须区分：

- 已在本机真实验证的功能；
- 仅完成静态实现但尚未真实验证的功能；
- 依赖外部应用、账号、网络或模型服务的功能；
- 计划中的后续增强功能。
