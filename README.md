# OpenHarmony Multimodal Test Agent

一个面向 OpenHarmony/HarmonyOS 的受控多模态 UI 测试 Agent：把自然语言任务转换为结构化计划，使用 HDC 采集截图与 UI 层级，通过 LLM/VLM 每轮选择一个类型化工具，保存可复盘 Run Trace，并从已验证动作确定性生成和回放 Hypium Driver 用例。

项目于 2026-09-09 完成真实模型、真实设备、Hypium 回放和 Web 全链路验收。详细安装与排错见 [docs/STARTUP_GUIDE.md](docs/STARTUP_GUIDE.md)。

Web 控制台于 2026-09 完全重写（浅色玻璃拟态界面，旧版冻结在 `web-legacy/`）：实时展示大模型的规划、视觉理解、逐步决策与探索顾问对话留痕，配闭环流水线状态条、探索页面状态图、历史运行回看与 Profile 资产管理，收敛为 5 个 Tab（会话 / 页面关系图 / 脚本与回放 / Profile 资产 / 历史运行），详见 [web/README.md](web/README.md)。

## 核心原则

- **模型负责理解与选择，不负责任意执行。** Agent 没有通用 Shell 工具。
- **HDC 只能经类型化 Adapter 使用。** 坐标、超时和危险语义在执行前校验。
- **每轮最多一个工具。** 失败、断言失败、设备断开和连续无变化会停止。
- **截图必须落到本地。** 每个 Action 关联前后 Snapshot、布局、SHA-256 和命令证据。
- **Hypium 确定性生成。** Python 使用固定模板，JSON 只写已验证字段；模型不能自由生成可执行代码。
- **运行产物不进入 Git。** Git 只管理 `artifacts/README.md`；经筛选的稳定 fixture 放在 `tests/fixtures/`。

## Agent 与模型配置

当前系统属于受控 UI 测试 Agent：编排器维护计划、页面状态和运行证据，模型每轮只输出一个结构化决策，HDC 与 Hypium 由白名单适配器执行。`AGENT_MODEL` 负责规划，`AGENT_VISION_MODEL` 负责截图理解和逐步决策；后者留空时自动复用前者，因此一个支持图像和结构化输出的模型即可覆盖全流程。两个阶段通过 `PlanResult`、`ScreenSnapshot` 和 `ToolDecision` 通信，没有模型间的直接聊天。

任务规划与逐步决策仍是独立请求；自动探索阶段的 LLM 视觉顾问在单次探索内维护一段连续对话（system 前缀稳定、逐页追加截图与候选），使"首页 → 搜索 → 热搜"这类连贯流程共享上下文，历史超过 `advisor_history_turns` 轮时保留 system 头并裁掉最旧交换，被裁页面的摘要由确定性上下文摘要继续携带。命中率仍由兼容服务决定，项目侧不统计缓存指标。
## 无预置 Profile 的应用发现

新运行可以只提供应用名称或 `bundleName`。系统通过 `bm dump` 解析已安装应用，以 `aa start` 启动并进行有界探索，生成 `draft → candidate → verified` Profile。探索默认限制为 20 个页面、每页 8 个动作、15 分钟；登录、授权、提交、发布和下载需要逐项启用，支付、删除、卸载和清除数据始终禁止。

探索治理（防过度探索）：

- **逻辑页身份**：页面按 `page_path + 前台 + 全量折叠 key + 可交互结构` 去重，信息流/热搜等"同页不同内容"的抖动不再被当成新页面。
- **内容候选抑制**：key/id 内嵌长数字 ID 或标题超长的可点击元素被降级到 input/swipe 之后，避免主动误触信息流卡片进入不可回放的内容页；此类跃迁不会入队。
- **类型配额**：每页动作预算按类型分配（click ≤ max-2、input 1、swipe 1），点击富集页面也能覆盖输入与滑动。
- **恢复节奏**：候选动作之间优先"落地页身份校验 → 一次 back → 冷启动重放"逐级兜底（input 动作因软键盘强制冷恢复）；只有队列出队回放路径时才必须冷启动。
- **LLM 视觉顾问**（配置了 vision 模型时默认开启）：每个新逻辑页向连续会话追加一次截图+候选询问，模型返回"推荐/回避"建议对确定性候选重排过滤；安全分类器在顾问之后照常执行，任何调用失败自动回退启发式排序。

```powershell
uv run main.py run --app "示例应用" --task "打开设置并验证版本信息" --execute
uv run main.py run --bundle-name com.example.app --task "验证首页" --execute
```

已有 `TARGET_PROFILE_PATH` 和 `--target-app` 继续作为兼容入口，并会输出弃用提示。Profile 验证要求单轮设备验证下至少 3 个可重复页面、`min_interaction_kinds`（默认 2，可配置至 3）类交互、3 个稳定定位器和 2 个应用级断言；随后 1 次 Hypium Driver 回放通过即自动晋级 `verified`（剩余 2 次由用户/CI 通过 `POST /api/profiles/{id}/replay` 异步追加，累计 3 次连续成功）。运行轨迹冻结目标与 Profile 快照，历史 Run 重新生成时不会读取后来变化的全局 Profile。

**资产流水线降级（无 Profile 执行，内部字段 `trace.live_mode` 不变）**：不再强制"先有 verified Profile 才能执行任务"。没有可用 Profile 且探索关闭、或探索/验证失败时，运行自动降级为资产流水线降级路径——每步现取截图决策继续执行任务，但不生成/回放 Hypium 脚本，前端在「会话」Tab 的「资产流水线降级」子视图中展示；`bootstrap_only` 显式生成 Profile 的运行仍会如实失败。

## 已实现能力

- `HarmonyDeviceAdapter`：连接、健康检查、启动应用、截图、`file recv`、布局、日志、点击、输入、滑动、返回和等待。
- UI 层级标准化、系统节点过滤、语义目标变体、运行时 `element_id`、稳定定位器优先和 VLM bbox 融合。
- Pydantic AI OpenAI-compatible Provider、实际 PNG 多模态输入和显式 Mock Provider。
- 规划对齐：不擅自把“输入文本”扩展为“提交搜索”；软键盘返回流程可被确定性补全。
- Agent 状态机、安全白名单、模型/设备独立超时、有限重试、停止和失败即停。
- 探索治理：结构身份去重、内容候选抑制、类型配额、back 优先恢复节奏与可配置准入门槛。
- LLM 视觉探索顾问：连续会话逐页约束点击范围与次数，启发式兜底，事件流留痕。
- 资产流水线降级：无 Profile 直接执行任务（探索失败自动降级），不生成/回放 Hypium 脚本。
- DC 会话 → 蒸馏 Profile：`POST /api/dc/sessions/{session_id}/profile/distill` 把覆盖 ≥3 个页面的 DC 会话经 1 轮设备验证 + 1 次 Hypium 回放蒸馏为 Profile；DC 工具由 23 个扩展到 26 个（新增 `assert_visible` / `assert_not_visible` / `assert_text` 断言工具）。
- SQLite + JSON/PNG/日志/HTML 报告、SSE 事件流和页面关系图。
- Hypium Python、JSON 与元数据生成；动态 key 前缀化；坐标降级显式告警。
- FastAPI 后端与 React/Vite/TypeScript 控制台。
- legacy `mvp_phase1` 保留为设备可行性探针，不再继续堆叠新业务。

## 快速开始

### 1. 首次安装

在仓库根目录执行：

```powershell
uv sync --all-groups
Copy-Item .env.example .env
uv run main.py dev --install
```

`uv.toml` 会自动把 uv 下载缓存放在项目内的 `.uv-cache`；`.venv` 仍是项目虚拟环境。`dev --install` 会执行前端的 `npm ci`，然后在同一终端启动 API 与 Web。后续启动省略 `--install` 即可。

`pyproject.toml` 的 `[project.scripts]` 会在 `uv sync` 时生成 `.venv\Scripts\harmony-test-agent.exe`，它只是转发到 `harmony_test_agent.cli:main` 的 Windows 包装器。Windows 会锁定正在运行的 `.exe`，导致另一个 uv 同步进程无法替换它，因此源码检出环境推荐使用不会锁定包装器的 `uv run main.py ...`。

在 `.env` 中填写模型、VLM、HDC 和设备配置。若兼容端点报 `Thinking mode does not support this tool_choice`，说明该端点的 thinking 模式拒绝强制 `tool_choice`：本项目所有结构化输出都已统一使用 `PromptedOutput`，请勿新增裸 `BaseModel` 的 `output_type`。`AGENT_DISABLE_THINKING=true` 只对 DeepSeek 官方端点有效，对 OpenAI 兼容自建端点会被忽略，不能用来规避该错误（详见 `docs/STARTUP_GUIDE.md` 5.2 节）。

### 2. 预检

```powershell
uv run main.py preflight
```

### 3. 运行真实 Agent

```powershell
$task = '打开知乎++，进入搜索，输入 OpenHarmony，返回首页，打开一条内容详情，确认页面存在可见内容后返回首页。'
uv run main.py run --provider openai --task $task
```

成功 Run 默认生成 Hypium 文件。也可单独执行：

```powershell
uv run main.py generate --run-id <run-id>
uv run main.py execute --run-id <run-id> --attempts 3
```

### 4. 启动 API 与 Web

推荐由一个终端统一管理两个服务：

```powershell
uv run main.py dev
```

需要自定义监听地址、端口或 API 热重载时：

```powershell
uv run main.py dev `
  --api-host 127.0.0.1 --api-port 18000 `
  --web-host 127.0.0.1 --web-port 15173 `
  --reload
```

输出以 `[api]`、`[web]` 标识来源。按一次 `Ctrl+C` 会同时停止两个子进程；任一服务异常退出时，启动器会停止另一个服务并返回非 0 退出码。`serve` 只启动 API，适合手动双终端排错。完整端口、CORS、`node_modules` 和残留进程排查见 [启动手册](docs/STARTUP_GUIDE.md)。

## CLI

```text
preflight   检查 Python、依赖、模型、Hypium、HDC、设备、截图和布局
run         执行自然语言任务，可选 real/mock、模式、设备、生成和回放
generate    从已有 Run Trace 重新生成 Hypium
execute     回放生成用例 1—3 次
dev         同时启动 FastAPI 与 Vite，可安装前端依赖并统一管理生命周期
serve       只启动 FastAPI/SSE
```

`dev` 支持 `--api-host`、`--api-port`、`--web-host`、`--web-port`、`--reload` 和 `--install`。运行模式固定为 `regression`（`exploration`、`stability`、`reproduction` 仅用于读取旧 `trace.json`）；当前完整验收链路是 `regression`。

## 架构

```text
CLI / FastAPI / React
        ↓
AgentOrchestrator
  ├─ Planning + Vision + ToolDecision (Pydantic AI)
  ├─ SafetyPolicy + ToolExecutor
  ├─ HarmonyDeviceAdapter (HDC)
  ├─ PerceptionService (UI hierarchy + VLM)
  ├─ PageGraphBuilder
  ├─ ArtifactStore + RunRepository
  └─ HypiumGenerator + HypiumRunner
```

详细设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 安全边界

Agent 工具白名单：

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

默认禁止登录、支付、验证码、删除、卸载、清除数据和授权。模型失败返回 `failed_model`，设备失败返回 `failed_device`，元素缺失返回 `failed_element`，断言失败返回 `failed_assertion`，不会伪装为成功或继续执行后续危险动作。

## 产物与 Git

本地证据位于：

```text
artifacts/preflight/
artifacts/runs/<run-id>/
artifacts/validation/
artifacts/agent.db
```

这些文件包含动态截图、布局 JSON、模型输出、日志、报告和数据库，体积大且依赖本机环境，因此由 `.gitignore` 排除。`artifacts/README.md` 只定义目录契约；可复现 fixture 位于 `tests/fixtures/legacy/zhihu-plus/`。

清理前先预览，并保留最新真实成功 Run 与最新预检：

```powershell
.\scripts\clean-runtime.ps1 `
  -Runs `
  -KeepLatestSuccessfulRun `
  -KeepLatestPreflight `
  -WebAcceptanceLogs `
  -ToolCaches `
  -WhatIf
```

确认预览清单后移除 `-WhatIf`。脚本拒绝项目外路径，且不会删除 `artifacts/phase1`。

## 质量门禁

```powershell
uv lock --check
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m pytest -q
Push-Location web
npm run build
npm run test
Pop-Location
git diff --check
```

真实 Provider 测试默认跳过，显式启用：

```powershell
$env:RUN_LIVE_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q tests\live\test_real_provider.py -s
```

## 2026-09-09 验收结果

- 预检：Python 3.14.0、Hypium 6.1.0.210、设备 `127.0.0.1:5555`、1320×2232、截图/布局均通过。
- 真实模型：`deepseek-v4-flash-vision-exp`，实际截图输入，`model_mock=false`。
- 最终 Run：`run-20260909T140205Z-e9ada52e`，18 个 Action、19 个 Snapshot、2 个通过断言、19 个状态节点、17 条边。
- Hypium：连续 3 次 `returncode=0`、`passed=true`。
- API/SSE：117 个事件按 ID 1—117 顺序读取，Graph/Script/Report/Artifact 均为 200。
- Web：live/graph/script/report 真浏览器通过；1440px 与 375px 无横向溢出；浏览器按钮触发三次回放并全部成功。
- 自动测试：33 passed、1 skipped；Ruff、uv lock、Vite production build 和 `git diff --check` 通过。

## 当前限制

- Hypium Driver 模式已真实验证；DevEco Testing 测试工程模式仍为 `not_validated`。
- 探索按"逻辑页身份"去重后，信息流抖动不再拆分页面；页面签名仍受 VLM 标题变化影响，仅作快照证据而非去重键。
- 无稳定 key/id 的控件会降级为经过边界验证的坐标，并在代码、元数据和 Web 中持续告警。
- 资产流水线降级不产出 Hypium 脚本与回放证据；需要回归脚本时仍需先完成 Profile 引导。
- FastAPI 当前是本机开发服务，没有鉴权，不应直接暴露到不受信任网络。
