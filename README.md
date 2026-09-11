# OpenHarmony Multimodal Test Agent

一个面向 OpenHarmony/HarmonyOS 的受控多模态 UI 测试 Agent：把自然语言任务转换为结构化计划，使用 HDC 采集截图与 UI 层级，通过 LLM/VLM 每轮选择一个类型化工具，保存可复盘 Run Trace，并从已验证动作确定性生成和回放 Hypium Driver 用例。

项目于 2026-09-09 完成真实模型、真实设备、Hypium 三次回放和 Web 全链路验收。详细安装与排错见 [docs/STARTUP_GUIDE.md](docs/STARTUP_GUIDE.md)。

## 核心原则

- **模型负责理解与选择，不负责任意执行。** Agent 没有通用 Shell 工具。
- **HDC 只能经类型化 Adapter 使用。** 坐标、超时和危险语义在执行前校验。
- **每轮最多一个工具。** 失败、断言失败、设备断开和连续无变化会停止。
- **截图必须落到本地。** 每个 Action 关联前后 Snapshot、布局、SHA-256 和命令证据。
- **Hypium 确定性生成。** Python 使用固定模板，JSON 只写已验证字段；模型不能自由生成可执行代码。
- **运行产物不进入 Git。** Git 只管理 `artifacts/README.md`；经筛选的稳定 fixture 放在 `tests/fixtures/`。

## Agent 与模型配置

当前系统属于受控 UI 测试 Agent：编排器维护计划、页面状态和运行证据，模型每轮只输出一个结构化决策，HDC 与 Hypium 由白名单适配器执行。`AGENT_MODEL` 负责规划，`AGENT_VISION_MODEL` 负责截图理解和逐步决策；后者留空时自动复用前者，因此一个支持图像和结构化输出的模型即可覆盖全流程。两个阶段通过 `PlanResult`、`ScreenSnapshot` 和 `ToolDecision` 通信，没有模型间的直接聊天。

当前每次模型调用都是独立请求，没有项目侧 Prompt Cache、共享消息历史或命中率指标。把两个配置统一为同一模型可以减少模型切换，但缓存是否命中仍由兼容服务和请求前缀稳定性决定，不能仅凭统一模型名推断命中率提升。
## 无预置 Profile 的应用发现

新运行可以只提供应用名称或 `bundleName`。系统通过 `bm dump` 解析已安装应用，以 `aa start` 启动并进行有界探索，生成 `draft → candidate → verified` Profile。探索默认限制为 20 个页面、每页 8 个动作、15 分钟；登录、授权、提交、发布和下载需要逐项启用，支付、删除、卸载和清除数据始终禁止。

```powershell
uv run main.py run --app "示例应用" --task "打开设置并验证版本信息" --execute
uv run main.py run --bundle-name com.example.app --task "验证首页" --execute
```

已有 `TARGET_PROFILE_PATH` 和 `--target-app` 继续作为兼容入口，并会输出弃用提示。Profile 验证要求三轮独立启动下至少 3 个可重复页面、3 类交互、3 个稳定定位器和 2 个应用级断言；随后三次 Hypium Driver 回放全部通过才自动晋级 `verified`。运行轨迹冻结目标与 Profile 快照，历史 Run 重新生成时不会读取后来变化的全局 Profile。

## 已实现能力

- `HarmonyDeviceAdapter`：连接、健康检查、启动应用、截图、`file recv`、布局、日志、点击、输入、滑动、返回和等待。
- UI 层级标准化、系统节点过滤、语义目标变体、运行时 `element_id`、稳定定位器优先和 VLM bbox 融合。
- Pydantic AI OpenAI-compatible Provider、实际 PNG 多模态输入和显式 Mock Provider。
- 规划对齐：不擅自把“输入文本”扩展为“提交搜索”；软键盘返回流程可被确定性补全。
- Agent 状态机、安全白名单、模型/设备独立超时、有限重试、停止和失败即停。
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

在 `.env` 中填写模型、VLM、HDC 和设备配置。若兼容端点报 thinking/tool choice 冲突，设置：

```dotenv
AGENT_DISABLE_THINKING=true
```

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

`dev` 支持 `--api-host`、`--api-port`、`--web-host`、`--web-port`、`--reload` 和 `--install`。运行模式枚举为 `regression`、`exploration`、`stability`、`reproduction`；当前完整验收链路是 `regression`。

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
- 页面签名会受到动态信息流和 VLM 标题变化影响，可能把一个语义页面拆成多个状态。
- 无稳定 key/id 的控件会降级为经过边界验证的坐标，并在代码、元数据和 Web 中持续告警。
- FastAPI 当前是本机开发服务，没有鉴权，不应直接暴露到不受信任网络。
