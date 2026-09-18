# OpenHarmony 多模态测试 Agent 启动手册

更新日期：2026-09-13

本文面向首次拿到仓库的开发者，覆盖从环境准备、真实模型与设备配置、预检、Agent 运行、Hypium 生成/回放，到 FastAPI/SSE/Web 控制台、测试、清理与排错的完整流程。

## 1. 当前交付边界

当前已真实验证的主链路是：

```text
自然语言任务
→ Pydantic AI 结构化规划
→ HDC 截图与 UI 层级
→ 多模态页面理解
→ 单工具受控执行
→ Run Trace 与页面图
→ 确定性 Hypium Driver 用例
→ 1 次内联真机回放 + 2 次异步追加
→ FastAPI/SSE/React Web 展示与触发回放
```

当前未声称完成的是 DevEco Testing“测试工程模式”。仓库已完整验证 Hypium `UiDriver` 模式，但没有可复用且经过本机成功/失败双基线验证的 DevEco Testing 工程模板。

## 2. 目录说明

```text
src/harmony_test_agent/       正式 Python 包
  agents/                     规划、VLM、工具决策与执行编排
  api/                        FastAPI 与 SSE
  devices/                    HDC Adapter、截图、布局、日志
  generation/                 Hypium Python/JSON/元数据生成
  graph/                      页面签名、节点和动作边
  perception/                 UI 层级标准化、VLM 融合和定位
  runner/                     Hypium 子进程与回放（1 次内联 + 异步追加）
  runtime/                    工具执行、安全策略、事件
  storage/                    SQLite 与本地产物
web/                          React + Vite + TypeScript 控制台
tests/unit/                   单元测试
tests/integration/            Fake Device + Mock Provider 集成测试
tests/api/                    API/SSE 契约测试
tests/live/                   显式启用的真实模型/设备测试
tests/fixtures/legacy/        经筛选、可复现、无敏感信息的 legacy fixture
profiles/zhihu-plus.json       默认演示应用的稳定配置
mvp_phase1/                   legacy 设备可行性探针，不再承载新业务
artifacts/                    本地运行证据；除 README 外不进入 Git
scripts/clean-runtime.ps1     带路径保护和 ShouldProcess 的清理脚本
```

## 3. 前置条件

### 3.1 必需软件

- Windows 10/11 与 PowerShell。
- Python 3.14。
- `uv`。
- Node.js 24 或兼容当前 Vite 7 的版本。
- DevEco/OpenHarmony SDK 中的 `hdc.exe`。
- 已启动并可被 HDC 识别的 OpenHarmony 模拟器或真机。
- 默认演示时，设备中已安装知乎++：`com.github.zhuoyi233.zhplus`，Ability 为 `EntryAbility`。

### 3.2 已验证基线

2026-09-09 的本机基线：

- Python：3.14.0
- `hypium`：6.1.0.210
- `pydantic-ai`：1.73.0
- 设备：`127.0.0.1:5555`
- 截图分辨率：1320 × 2232
- 应用：知乎++

版本以 `pyproject.toml` 与 `uv.lock` 为准，不要手工安装另一个 Hypium 版本覆盖虚拟环境。

## 4. 首次安装

在仓库根目录执行：

```powershell
uv sync --all-groups
Copy-Item .env.example .env
uv run main.py dev --install
```

`uv.toml` 会自动把 Python 包下载缓存放在仓库内的 `.uv-cache`。项目虚拟环境仍位于 `.venv`。如果不准备立即启动服务，可以先省略最后一条命令；之后执行 `uv run main.py dev --install` 安装锁文件指定的 Web 依赖并启动 API 与 Web。

验证 CLI：

```powershell
uv run main.py --help
uv run main.py dev --help
```

`pyproject.toml` 中的 `[project.scripts]` 声明了：

```toml
harmony-test-agent = "harmony_test_agent.cli:main"
```

`uv sync` 会据此生成 `.venv\Scripts\harmony-test-agent.exe`。这个文件是 Windows 命令包装器，负责加载项目环境并调用同一个 `main()`，不包含另一套业务逻辑。若包装器正在运行，Windows 会锁定该 `.exe`；此时另一个 `uv run harmony-test-agent ...` 触发同步并尝试更新包装器，可能报 `Access is denied`。源码检出环境统一使用 `uv run main.py ...`，可以避免把正在执行的包装器作为同步更新目标。

## 5. 配置 `.env`

复制模板：

```powershell
Copy-Item .env.example .env
```

`.env` 已被 Git 忽略。不得把真实 API Key 写进 `.env.example`、Profile、测试 fixture、日志或文档。

推荐配置：

```dotenv
OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
OPENAI_API_KEY=your-secret-key
AGENT_MODEL=your-structured-output-model
AGENT_VISION_MODEL=your-vision-model
AGENT_PROVIDER=auto
AGENT_DISABLE_THINKING=false
HDC_PATH=C:\path\to\openharmony\toolchains\hdc.exe
HARMONY_DEVICE=127.0.0.1:5555
RUNTIME_DIR=artifacts/runs
RUNTIME_HOME=.runtime-user
DATABASE_PATH=artifacts/agent.db
TARGET_PROFILE_PATH=profiles/zhihu-plus.json
AGENT_MAX_STEPS=20
AGENT_ACTION_TIMEOUT=30
AGENT_MODEL_TIMEOUT=90
AGENT_RETRY_LIMIT=2
UNCHANGED_SCREEN_LIMIT=2
VLM_MIN_CONFIDENCE=0.55
```

### 5.1 配置项说明

| 配置项                     | 说明                                                                                 |
| -------------------------- | ------------------------------------------------------------------------------------ |
| `OPENAI_BASE_URL`        | OpenAI-compatible API 根地址。由所用服务商决定是否包含`/v1`。                      |
| `OPENAI_API_KEY`         | 本地密钥，只保存在忽略的`.env`。                                                   |
| `AGENT_MODEL`            | 规划模型；必须支持当前 Provider 的结构化输出。                                       |
| `AGENT_VISION_MODEL`     | 截图理解和工具决策模型；留空时回退到`AGENT_MODEL`。                                |
| `AGENT_PROVIDER`         | `auto`、`openai` 或 `mock`。`auto` 在配置完整时使用真实模型，否则使用 Mock。 |
| `AGENT_DISABLE_THINKING` | 追加 `extra_body.thinking.type=disabled`；**只对 DeepSeek 官方端点有效**，自建 OpenAI 兼容端点会忽略，不能用来规避 `tool_choice` 冲突（见 5.2 节）。                     |
| `HDC_PATH`               | `hdc.exe` 的绝对路径；若 HDC 已在 `PATH` 中可留空。                              |
| `HARMONY_DEVICE`         | 设备序列号，默认`127.0.0.1:5555`。                                                 |
| `AGENT_ACTION_TIMEOUT`   | HDC/设备动作超时，默认 30 秒。                                                       |
| `AGENT_MODEL_TIMEOUT`    | 规划、VLM 和工具决策超时，默认 90 秒。不要与设备动作超时混用。                       |
| `AGENT_RETRY_LIMIT`      | 瞬时动作失败的有限重试次数；断言和元素缺失默认立即停。                               |
| `UNCHANGED_SCREEN_LIMIT` | 连续可变动作截图不变化的停止阈值，默认 2。                                           |
| `VLM_MIN_CONFIDENCE`     | VLM 视觉元素进入融合列表的最低置信度，默认 0.55。                                    |
| `RUNTIME_HOME`           | Hypium/xdevice 的项目内隔离 Home，默认`.runtime-user`。                            |
| `PROFILE_VERIFICATION_ROUNDS` | Profile 设备验证轮次，默认 1；设为 `3` 可回滚到重构前的三轮独立启动验证。       |
| `HYPIUM_REPLAY_ATTEMPTS` | 主流程内联 Hypium 回放次数，默认 1；设为 `3` 可回滚到重构前的三次内联回放。剩余次数由 `POST /api/profiles/{id}/replay` 异步追加。 |

自动探索的 LLM 视觉顾问在 `AGENT_VISION_MODEL`（或回退的 `AGENT_MODEL`）可用且 `AGENT_PROVIDER` 非 mock 时默认启用，逐页约束点击范围与次数；配置缺失或调用失败时自动回退纯启发式探索，无需额外环境变量。顾问的每次探索会话与逐页建议记录在 `artifacts/runs/<run_id>/discovery/summary.json` 的 `advisor_turns`/`advisor_verdicts` 中；每次 LLM 调用的输入（页面截图 + 编号候选 payload）与输出（结构化建议）还会以 `advisor_log` 留痕，并经 `discovery_progress`（`stage=advisor_turn`）事件实时推送，Web 控制台「会话」Tab 的「顾问对话」子视图实时累积这些事件（探索进行中即可见），并与探索结束后的 `advisor_log` 按轮次合并展示。`advisor_verdicts` 每条附带 `candidates` 可读摘要（编号/kind/label/coordinate），把 recommended/avoid 下标映射回控件文本。

探索完成（如 `停止原因 admission_metrics_reached`，即"已达准入指标，探索提前完成"）后，编排器继续执行 Profile 验证、脚本生成与准入回放：默认只做 1 轮设备验证（`PROFILE_VERIFICATION_ROUNDS`）和 1 次内联 Hypium 回放（`HYPIUM_REPLAY_ATTEMPTS`），验证阶段逐轮推送 `profile_verification_round_started/finished`，回放阶段逐次推送 `hypium_replay_started/finished`，实时视图全程有反馈，不会出现"已停止却仍在运行"的观感。剩余 2 次回放由用户/CI 通过 `POST /api/profiles/{id}/replay` 在设备空闲时异步追加，满足「3 次连续成功」要求而不阻塞主流程。

### 5.2 thinking 兼容性

若真实模型返回：

```text
Thinking mode does not support this tool_choice
```

说明该端点把模型路由到 thinking 模式，而请求里带了**强制 tool_choice**（`required` 或指定函数）。DeepSeek 侧明确拒绝这个组合，`isRetryable=false`，重试无用。

本项目所有结构化输出都已统一走 `PromptedOutput`（`prompted` 模式：不注册工具、不发送 `tool_choice`），因此**正常情况下不会再出现该错误**。仍然报错时按下面逐项排查：

| 成因 | 处置 |
| --- | --- |
| 代码里新写了 `output_type=<某个 BaseModel>` 裸传（pydantic-ai 会走 `tool` 模式并发 `tool_choice=required`） | 改回 `OpenAICompatibleProvider._structured_output(...)`，不要绕过它 |
| 在 pydantic-ai 之外自行拼请求并设置了 `tool_choice` | 去掉强制值，最多用 `auto`；thinking 端点不接受 `required` 和指定函数 |

`AGENT_DISABLE_THINKING=true` 只向端点追加 `extra_body.thinking.type=disabled`，**它只对 DeepSeek 官方端点有效**。对 OpenAI 兼容的自建端点（例如 `api.commandcode.ai/provider/v1`）该字段会被忽略，thinking 依然开启；这类端点也没有可用的关闭手段（其 `reasoning_effort` 只接受 `low`/`medium`/`high`/`xhigh`/`max`，没有 off）。因此**不要指望用它规避 `tool_choice` 冲突**，唯一正确的做法是结构化输出不使用工具。2026-09-09 的 DeepSeek 兼容端点验收使用了该设置。

## 6. 准备设备与应用

先检查设备：

```powershell
& $env:HDC_PATH list targets
```

如果没有在当前 PowerShell 设置 `HDC_PATH`，直接使用 `.env` 中的绝对路径，或执行：

```powershell
& 'C:\path\to\hdc.exe' list targets
```

期望看到：

```text
127.0.0.1:5555
```

应用 Profile 位于 `profiles/zhihu-plus.json`。默认 Profile：

- 只执行匿名、只读流程；
- 通过 stop/start 重置应用，不清除应用数据；
- 禁止登录、验证码、支付、删除和授权；
- 稳定定位器只保存人工确认的 key/id；
- 动态卡片、页面文本和 VLM bbox 只进入 Run Trace，不回写 Profile。

## 7. 运行预检

完整预检：

```powershell
uv run main.py preflight
```

不采集截图：

```powershell
uv run main.py preflight --no-screenshot
```

完整预检依次检查：

1. Python 版本；
2. 必需依赖可导入；
3. 模型与 VLM 配置是否完整；
4. Hypium 版本和导入；
5. HDC 与设备连接；
6. 实际分辨率；
7. 设备端截图、`hdc file recv`、Pillow 解码、SHA-256；
8. UI 层级元素数量。

报告位置：

```text
artifacts/preflight/preflight-report.json
artifacts/preflight/hypium-baseline.json
artifacts/runs/preflight-<timestamp>/screens/
artifacts/runs/preflight-<timestamp>/layouts/
```

任一必需项失败时 CLI 返回非 0；API Key 只显示为 `configured`，不会写入报告。

## 8. 运行 Agent

### 8.1 真实模型完整任务

```powershell
$task = '打开知乎++，进入搜索，输入 OpenHarmony，返回首页，打开一条内容详情，确认页面存在可见内容后返回首页。'
uv run main.py run `
  --provider openai `
  --mode regression `
  --max-steps 20 `
  --task $task
```

默认会在 Agent 成功后生成 Hypium Python、JSON 与元数据。加 `--no-generate` 可只执行 Agent；加 `--execute` 会在同一命令中生成并内联回放 1 次（`HYPIUM_REPLAY_ATTEMPTS`，设为 `3` 恢复内联 3 次）。

### 8.2 Mock 模式

无模型 Key 时验证状态机、HDC、存储和生成器：

```powershell
uv run main.py run `
  --provider mock `
  --task '打开知乎++，进入搜索，输入 OpenHarmony，返回首页。'
```

Mock 模式会明确记录 `model_mock=true`，不得把它当成真实多模态成功证据。

### 8.3 判定一次 Run 是否真正成功

不要只看 `state=completed`。至少核对：

- `model_mock=false`；
- 计划没有提前 `finish`；
- 每个 Action 都有 `before_snapshot_id` 和 `after_snapshot_id`；
- 点击、输入、返回和断言确实发生；
- 断言均为 `passed=true`；
- 页面图至少包含 3 个状态和 2 条边；
- `generated` 已存在；
- 失败后没有继续执行危险动作。

产物目录：

```text
artifacts/runs/<run-id>/
  trace.json
  graph.json
  screens/
  layouts/
  commands/
  generated/
  hypium/
  reports/report.json
  reports/report.html
```

## 9. 单独生成与回放 Hypium

若已存在成功 Run：

```powershell
$runId = 'run-xxxxxxxxTxxxxxxZ-xxxxxxxx'
uv run main.py generate --run-id $runId
uv run main.py execute --run-id $runId --attempts 3
```

每次回放使用独立目录：

```text
artifacts/runs/<run-id>/hypium/attempt-01/
artifacts/runs/<run-id>/hypium/attempt-02/
artifacts/runs/<run-id>/hypium/attempt-03/
```

每个目录保存：

- `command.json`
- `environment.json`
- `stdout.log`
- `stderr.log`
- `task_log.log`
- `generated_result.json`
- `final.jpeg` 或 `failure.jpeg`

Hypium 子进程把 `HOME`、`USERPROFILE` 指向仓库内 `.runtime-user`，避免写入用户全局 `.xdevice`。生成器会把动态长数字 key 转换为 `MatchPattern.STARTS_WITH`。没有稳定 key/id 的控件只能使用经过验证的坐标，并同时写入生成代码注释、JSON/元数据告警和 Web 告警区。

## 10. 启动 API 与 Web 控制台

### 10.1 推荐：单终端启动

日常开发从仓库根目录运行：

```powershell
uv run main.py dev
```

`dev` 同时启动 FastAPI 和 Vite，并把两个进程的输出转发到当前终端。每行以 `[api]` 或 `[web]` 开头，可以直接判断日志来源。默认地址为：

```text
API  http://127.0.0.1:8000
Web  http://127.0.0.1:5173/
```

首次安装或需要按 `web/package-lock.json` 重新安装前端依赖时增加 `--install`：

```powershell
uv run main.py dev --install
```

自定义地址、端口并启用 API 热重载：

```powershell
uv run main.py dev `
  --api-host 127.0.0.1 `
  --api-port 18000 `
  --web-host 127.0.0.1 `
  --web-port 15173 `
  --reload
```

参数含义：

```text
--api-host   FastAPI 监听地址
--api-port   FastAPI 监听端口
--web-host   Vite 监听地址
--web-port   Vite 监听端口
--reload     启用 Uvicorn 源码热重载
--install    启动前先在 web 目录执行 npm ci
```

按一次 `Ctrl+C` 后，启动器会向 API 和 Web 子进程发送停止信号，等待它们退出，并清理仍存活的子进程。按 `Ctrl+C` 中断返回退出码 130；API 或 Web 无法启动、运行中异常退出，或者依赖安装失败时返回非 0，同时停止另一项服务。自动化脚本应检查 `$LASTEXITCODE`，不能只看到其中一个服务打印过启动日志就判定成功。

### 10.2 `serve` 与手动双终端排错

`serve` 只启动 FastAPI/SSE，不会安装或启动 Web：

```powershell
uv run main.py serve --host 127.0.0.1 --port 8000
```

当需要分别观察原始日志、单独重启 Vite 或确认问题属于哪一侧时，使用两个终端。

终端 1：

```powershell
uv run main.py serve --host 127.0.0.1 --port 8000 --reload
```

终端 2：

```powershell
Push-Location web
$env:VITE_API_URL = 'http://127.0.0.1:8000'
npm run dev -- --host 127.0.0.1 --port 5173
Pop-Location
```

手动模式下两个进程互不管理，停止时要在两个终端分别按 `Ctrl+C`。`VITE_API_URL` 必须在 Vite 启动前设置。

### 10.3 端口与 CORS

若出现 `WinError 10013`、`listen EACCES` 或地址已被占用，先检查 Windows 排除端口和当前监听进程：

```powershell
netsh interface ipv4 show excludedportrange protocol=tcp
Get-NetTCPConnection -State Listen | Where-Object LocalPort -In 8000,5173
Get-Process -Id (Get-NetTCPConnection -State Listen -LocalPort 8000).OwningProcess
```

选择未被排除且未被占用的端口，再通过 `dev` 的 `--api-port`、`--web-port` 同时传入。浏览器报 CORS 时，先确认 Web 实际来源与 API 配置允许的来源完全一致，包括协议、主机和端口；`localhost` 与 `127.0.0.1` 属于不同来源。默认允许本机的 `5173`，验收端口 `15173` 也已列入允许来源。统一 `dev` 启动器会自动把所选 Web 来源传给 FastAPI；手动改用其他 Web 端口时，在启动 API 前设置 `HARMONY_CORS_ORIGINS=http://127.0.0.1:<port>`，多个来源用逗号分隔。

### 10.4 npm、`node_modules` 与残留进程

`dev` 报找不到 `npm` 时检查 Node.js 与 npm 是否在 `PATH`：

```powershell
node --version
npm --version
```

`web/node_modules` 不存在、依赖不完整或 lockfile 更新后，重新运行：

```powershell
uv run main.py dev --install
```

`--install` 使用 `npm ci`，要求 `web/package-lock.json` 与 `web/package.json` 一致。若安装失败，先处理 npm 输出的 lockfile、网络或权限错误；启动器会保留非 0 退出码。

按 `Ctrl+C` 后端口仍被占用，通常表示之前从另一个终端启动了服务，或父终端被直接关闭而留下子进程。按端口定位进程，并核对命令行后再停止：

```powershell
$connection = Get-NetTCPConnection -State Listen -LocalPort 8000
Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)" |
  Select-Object ProcessId, Name, CommandLine
Stop-Process -Id $connection.OwningProcess
```

对 Web 端口重复检查。不要按名称批量结束所有 `python` 或 `node`，这会影响其他项目。

### 10.5 Web 功能（v2 重写控制台）

Web 控制台（2026-09 重写，架构见 `web/README.md`）支持：

- 左栏启动器：目标解析（应用名/bundleName）、自然语言任务、固定运行模式徽章「回归测试」、探索策略编辑、启动/停止；
- 闭环流水线状态条：采集 → 感知 → 规划 → 探索 → 验证 → 脚本 → 回放 → 报告 随运行实时点亮；
- **Tab 收敛为 5 个**：会话（DC 为主 +「资产流水线降级」Live 子视图 +「顾问对话」子视图）、页面关系图、脚本与回放、Profile 资产、历史运行；
- **会话页（DC）**：DC 工具调用与对话流、脚本生成、「蒸馏为 Profile」按钮；
- 会话页内的资产流水线降级子视图（原 Live Mode）：**思考流**（模型规划、视觉摘要、逐步工具决策、顾问建议的聚合时间线）
  与**事件日志**（每条事件可展开查看原始 payload JSON）双视图切换、设备画面、当前元素表；
- **顾问对话子视图**：探索顾问每轮 LLM 调用的输入（截图 + 候选列表）与输出（结构化建议）回放，
  探索进行中即实时累积，旧运行自动回退展示逐页结论；
- 页面关系图：任务阶段 trace 图优先，探索型运行自动回退到探索页面状态图；
- 历史运行列表（左栏 + 完整表格），点击任意历史 Run 即可回看轨迹/图/脚本/报告；
- Profile 资产管理：快速复验、锁定/解锁、回退、失效、手动追加回放（回放进度显示「已记录回放数/3」）；
  启动运行时对 verified Profile 自动快速复验，失败仅记录证据并转入完整探索重新验证，不再自动失效，失效为手动操作；
- Hypium Python、生成告警、验收回放进度；内嵌 HTML 报告与下载。

可用深链接直接查看 Run（tab 值为会话/页面关系图/脚本与回放/Profile 资产/历史运行；旧值
`live`、`advisor`、`dc` 兼容映射到 `session`，`report` 映射到 `script`）：

```text
http://127.0.0.1:5173/?run_id=<run-id>&tab=session
http://127.0.0.1:5173/?run_id=<run-id>&tab=graph
http://127.0.0.1:5173/?run_id=<run-id>&tab=script
http://127.0.0.1:5173/?run_id=<run-id>&tab=profiles
http://127.0.0.1:5173/?run_id=<run-id>&tab=runs
```

## 11. CLI、API 与 SSE

CLI 子命令：

```text
preflight   检查 Python、依赖、模型、Hypium、HDC、设备、截图和布局
run         执行自然语言 UI 测试任务
generate    从已有 Run Trace 生成 Hypium 项目
execute     回放生成用例 1—3 次
dev         同时启动 API 与 Web，统一转发日志并管理进程生命周期
serve       只启动 FastAPI/SSE
```

主要接口：

```text
GET  /api/health/live
GET  /api/health
GET  /api/devices
GET  /api/runs
POST /api/runs
GET  /api/runs/{run_id}
POST /api/runs/{run_id}/stop
GET  /api/runs/{run_id}/events
GET  /api/runs/{run_id}/graph
GET  /api/runs/{run_id}/script
GET  /api/runs/{run_id}/report
POST /api/runs/{run_id}/generate
POST /api/runs/{run_id}/execute?attempts=3
GET  /api/runs/{run_id}/artifacts/{path}
GET  /api/profiles
POST /api/profiles/{profile_id}/verify
POST /api/profiles/{profile_id}/replay
POST /api/dc/sessions/{session_id}/profile/distill
```

`GET /api/health/live` 是轻量进程存活探针，不访问设备或外部模型，适合启动器和自动化轮询。`GET /api/health` 返回模型配置、设备连通性和 Hypium 状态，检查范围更完整，响应时间也可能受 HDC 影响。

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/health/live'
Invoke-RestMethod 'http://127.0.0.1:8000/api/health'
```

资产流水线（2026-09-17 重构）新增两个端点：

- `POST /api/profiles/{profile_id}/replay`：请求体 `{"attempts": 1..3}`（默认 1），在设备空闲时**异步追加** Hypium 回放证据，不阻塞主流程。主流程已内联 1 次（`HYPIUM_REPLAY_ATTEMPTS`），因此再追加 2 次即可满足「3 次连续成功」。candidate 累计满门禁次数且全部通过后自动晋级 `verified`；已 `verified` 的 Profile 只追加审计证据，状态与门禁资产不变；`locked` / `draft` / `invalid` 一律拒绝（409 / 422）。Profile 卡片显示「已记录回放数/3」进度条。
- `POST /api/dc/sessions/{session_id}/profile/distill`：请求体 `{"bundle_name": ..., "main_ability": ...}`，把 DC 会话蒸馏为 Profile，响应 `DcDistillResult{profile_id, status, pages_covered, stable_locators, assertions, replay_run_id, replay_passed, warnings}`。前置条件：会话覆盖 ≥3 个不同 `page_path`，且 bundle / ability 非占位值（否则 422）；无录制操作返回 409。流程为纯 CPU 提取（<1s）→ 1 轮设备验证 → 1 次 Hypium 回放 → promote。

```powershell
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/api/profiles/com.example.app/replay' `
  -ContentType 'application/json' -Body '{"attempts": 2}'
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/api/dc/sessions/<session-id>/profile/distill' `
  -ContentType 'application/json' -Body '{"bundle_name":"com.example.app","main_ability":"EntryAbility"}'
```

增量契约字段（2026-09，纯追加、不影响旧客户端）：`GET /api/runs/{id}/discovery` 返回 `advisor_log`（顾问逐轮输入/输出留痕）、`pages`/`transitions`（探索页面与跳转明细）；`advisor_verdicts[*]` 追加 `candidates` 摘要；`discovery_progress` 事件 payload 追加 `stage` 标识，`screen_captured` 事件 payload 追加 `summary`（视觉模型页面理解）。事件类型追加 `profile_verification_started`、`profile_verification_round_started`、`hypium_replay_started`（验证/回放过程逐轮逐次推送）；探索期每帧截图会实时发送 `screen_captured`/`elements_detected` 并追加 `trace.snapshots`，驱动实时页的设备画面与元素表。2026-09-17 再追加 DC 蒸馏事件 `profile_distill_started` / `profile_distill_finished` / `profile_distill_failed`。

SSE 示例：

```powershell
Invoke-WebRequest 'http://127.0.0.1:8000/api/runs/<run-id>/events?after=0'
```

事件包含 `id`、`event` 和 JSON `data`。客户端断线后可通过 `after=<last_event_id>` 继续获取。

## 12. 测试和质量门禁

### 12.1 静态检查与自动测试

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

`ruff` 会检查正式包、`mvp_phase1` 和 smoke 代码；`artifacts` 被明确排除。

### 12.2 显式真实 Provider 测试

该测试会调用外部模型并操作已连接设备，默认跳过：

```powershell
$env:RUN_LIVE_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q tests\live\test_real_provider.py -s
Remove-Item Env:RUN_LIVE_TESTS
```

测试会保存本地截图和结构化模型结果到：

```text
artifacts/validation/live-provider-smoke/
```

## 13. 运行产物与 Git 边界

Git 只跟踪 `artifacts/README.md`，意思不是“项目不保存产物”，而是：

- Run Trace、模型响应、PNG/JPEG、布局 JSON、SQLite、stdout/stderr 属于本机运行证据；
- 它们可能很大、频繁变化或包含环境信息，不适合作为源码版本历史；
- 经人工筛选、稳定、无敏感信息且用于测试的样本放在 `tests/fixtures/legacy/zhihu-plus/` 并进入 Git；
- `artifacts/phase1` 作为历史原始证据保留在本地，不由 Git 管理。

检查 Git 边界：

```powershell
git status --short
git check-ignore -v artifacts\agent.db
```

不要使用 `git add -f artifacts/...` 强制提交运行证据。

## 14. 安全清理

清理脚本支持 `-WhatIf` 和 PowerShell ShouldProcess，并拒绝项目根目录外的路径。

### 14.1 保留最终证据，只删除旧 Run 和 Web 临时日志

先预览：

```powershell
$finalRun = 'run-20260909T140205Z-e9ada52e'
.\scripts\clean-runtime.ps1 `
  -Runs `
  -KeepRunId $finalRun `
  -KeepLatestPreflight `
  -WebAcceptanceLogs `
  -WhatIf
```

也可以不写固定 Run ID，让脚本从 `trace.json` 中保留最新的 `completed` 且 `model_mock=false` 的 Run：

```powershell
.\scripts\clean-runtime.ps1 `
  -Runs `
  -KeepLatestSuccessfulRun `
  -KeepLatestPreflight `
  -WebAcceptanceLogs `
  -WhatIf
```

必须检查 `What if:` 输出确实只包含准备删除的目录。确认后移除 `-WhatIf`：

```powershell
.\scripts\clean-runtime.ps1 `
  -Runs `
  -KeepLatestSuccessfulRun `
  -KeepLatestPreflight `
  -WebAcceptanceLogs `
  -Confirm
```

`-WebAcceptanceLogs` 只删除 `artifacts/web-acceptance` 下的非 PNG 文件，保留浏览器截图。

### 14.2 其他清理选项

```powershell
# 只清理工具缓存和 Web build
.\scripts\clean-runtime.ps1 -ToolCaches -WhatIf

# 删除预检报告或 Provider 验证证据
.\scripts\clean-runtime.ps1 -Preflight -Validation -WhatIf

# 删除 SQLite 及 journal
.\scripts\clean-runtime.ps1 -Database -WhatIf
```

### 14.3 高风险选项

以下命令在没有 keep 参数时会删除整个 `artifacts/runs`：

```powershell
.\scripts\clean-runtime.ps1 -Runs -WhatIf
```

`-AllGenerated` 会选择 Runs、Preflight、Validation、Web 临时日志、Database 和 ToolCaches，应始终先运行：

```powershell
.\scripts\clean-runtime.ps1 -AllGenerated -WhatIf
```

脚本刻意不删除 `artifacts/phase1`。`-KeepRunId` 会验证目录存在且拒绝路径分隔符；`-KeepLatestPreflight` 或 `-KeepLatestSuccessfulRun` 找不到可保留目录时会先失败，不会继续删除。

## 15. 常见问题

### 15.1 `dev` 启动后立即退出

根据最后一条 `[api]` 或 `[web]` 日志判断失败进程，并检查 PowerShell 的 `$LASTEXITCODE`。`[web]` 提示缺少包时运行 `uv run main.py dev --install`；监听失败时按 10.3 节更换端口；其中一项失败后另一项被停止属于预期的生命周期清理。

### 15.2 按 `Ctrl+C` 后仍有服务占用端口

先用 `Get-NetTCPConnection` 与 `Get-CimInstance Win32_Process` 确认占用进程的命令行。常见原因是之前用手动双终端启动过服务，或直接关闭终端留下了进程。确认 PID 后仅停止对应进程。

### 15.3 浏览器能打开 Web，但请求 API 失败

确认 `VITE_API_URL` 指向实际 API 地址，再检查浏览器控制台是否为 CORS 错误。协议、主机或端口任一不同都会形成新来源；自定义 Web 端口必须出现在 FastAPI 的 CORS 允许列表中。先调用 `/api/health/live` 可区分 API 未启动与设备健康检查较慢。

### 15.4 `Thinking mode does not support this tool_choice`

该端点的 thinking 模式不接受任何强制 `tool_choice`。先确认没有代码把裸 `BaseModel` 传给 `Agent(output_type=...)`——所有结构化输出必须经 `OpenAICompatibleProvider._structured_output(...)`（内部是 `PromptedOutput`）。不要用 `AGENT_DISABLE_THINKING` 处理该错误：它只对 DeepSeek 官方端点有效，对 OpenAI 兼容的自建端点会被忽略，详见 5.2 节。

### 15.5 模型请求在 30 秒失败

确认使用的是 `AGENT_MODEL_TIMEOUT`，推荐从 90 秒开始；`AGENT_ACTION_TIMEOUT` 只控制设备动作。若端点本身响应慢，可临时提高到 120—180 秒，但上限为 300 秒。

### 15.6 `failed_model`

检查 Base URL、Key、模型名、结构化输出兼容性和 VLM 图片输入。系统不会在真实模型失败后自动伪装为 Mock 成功。

### 15.7 `failed_device`

检查模拟器是否启动、序列号是否变化、HDC 是否能执行 `list targets`。设备断开后状态机停止，不继续后续操作。

### 15.8 `failed_element`

优先检查对应 `layouts/`、`screens/` 与 `trace.json`。定位优先级为 key/id、精确文本、类型+文本、空间关系、VLM bbox、裸坐标。模型返回的运行时 `element_id` 可在当前 Snapshot 内精确解析，但不能直接作为稳定 Hypium 文本定位器。

### 15.9 断言失败

断言支持 UI 元素、语义目标简化和 VLM 页面摘要证据，但不会无限放宽。失败后立即停是设计行为，应查看失败截图，而不是关闭门禁。

### 15.10 Hypium 写入用户目录

只能通过本项目 Runner 启动生成用例；Runner 会隔离 `HOME` 和 `USERPROFILE`。不要直接用系统 Python 运行且同时修改全局 HOME。

### 15.11 页面图节点很多

当前页面签名综合页面路径、控件、文本与感知结果。动态信息流和 VLM 页面标题变化可能把同一语义页面拆成多个状态。当前验收满足页面/边证据要求，但后续仍可增加语义聚类以减少过度分裂。

### 15.12 应用名称解析失败

HarmonyOS NEXT 设备上 `bm dump -n` 返回的 `label` 是未解析的资源引用（如 `$string:app_name`），不是桌面显示的应用名，因此按"已安装应用名称"查找时不能依赖 `bm` 输出。当前实现改为通过 `uitest dumpLayout` 扫描启动器桌面：图标文本是系统解析后的本地化名称，bundle 名嵌在图标祖先节点 test key 中，二者配对建立"显示名 → bundle"映射。注意事项：

- 名称解析会把设备带回桌面（按 Home 键）并逐页左滑扫描，最多 6 页；`bundleName` 查找无此副作用。
- 桌面上没有图标的应用无法按名称解析，此时回退全量目录精确匹配后返回 not found；改用 `--bundle-name` 即可。

## 16. 当前限制

- 不处理登录、支付、验证码、删除、卸载、清除数据和授权类动作。
- 默认演示依赖知乎++已安装，核心接口本身应用无关。
- VLM bbox 必须通过置信度、面积和边界检查；无法确认时会失败而非盲点。
- 信息流和内容卡片是动态数据，回放使用 key 前缀或受告警坐标。
- Hypium Driver 模式已验证；DevEco Testing 测试工程模式仍为 `not_validated`。
- API 目前面向本机开发，没有鉴权，不应直接暴露到不受信任网络。
- `bm dump` 的 label 在新系统版本上是资源引用；应用名称查找依赖启动器桌面扫描，图标 key 格式随系统版本可能变化，解析失败时安全回退到全量目录精确匹配。

## 17. 2026-09-09 最终验收记录

- 预检：Python 3.14.0、Hypium 6.1.0.210、设备 `127.0.0.1:5555`、1320×2232、截图传输和 56 个 UI 元素全部通过。
- 真实 Provider smoke：实际 PNG 发送到 `deepseek-v4-flash-vision-exp`，`mock=false`，返回结构化页面理解和工具决策。
- 最终 Run：`run-20260909T140205Z-e9ada52e`，`completed`、`model_mock=false`、18 个动作、19 张 Snapshot、2 个通过断言、19 个页面状态、17 条边。
- Hypium：3 次回放均 `returncode=0`、`passed=true`；最终保存的耗时约为 29.2 秒、26.3 秒、26.9 秒。
- Web：真实浏览器检查 live/graph/script/report，页面图 19 节点、脚本 3 条告警、报告 iframe 正常；1440px 和 375px 均无横向溢出，浏览器控制台和请求失败为 0。
- Web 回放：浏览器按钮触发 `POST /execute?attempts=3`，API 返回 200，3 次均成功。
- 自动门禁：33 passed、1 skipped（显式 live 测试默认跳过）；Ruff、uv lock、Vite build、`git diff --check` 全部通过。


## 17. 测试陌生应用并自动生成 Profile

设备已安装应用后，不需要先编写 `profiles/*.json`：

```powershell
uv run main.py run --app "应用显示名称" --task "启动应用，探索可达页面，验证返回和重启恢复" --execute
# 显示名有多个匹配时，使用返回的 bundleName 精确重试
uv run main.py run --bundle-name com.example.app --task "验证首页和设置页" --execute
```

默认允许普通导航、滑动、返回、固定测试文本输入和只读断言。按需添加 `--allow-login`、`--allow-permission`、`--allow-submit`、`--allow-publish`、`--allow-download`；支付、删除、卸载和清除数据没有开放开关。可用 `--max-pages`、`--max-actions-per-page` 和 `--max-duration` 收紧探索上限。

运行目录保存 catalog 原始输出、启动探测、截图、布局、页面图、动作与门禁结果。Profile 先写 draft，1 轮设备验证后写 candidate，1 次内联 Hypium Driver 回放通过后自动写 verified；其余 2 次回放通过 `POST /api/profiles/{id}/replay` 异步追加（累计 3 次连续成功）。正式 Profile 可通过 API 锁定；更新时保存时间戳历史备份。`TARGET_PROFILE_PATH` 仍可显式使用旧 Profile。当前执行模式为 Hypium Driver；DevEco Testing 测试工程模式继续标记为 `not_validated`。


### Profile 管理 API

`GET /api/profiles` 查看 draft、candidate、verified；`POST /api/profiles/{id}/verify` 发起复验 Run；`POST /api/profiles/{id}/replay` 异步追加 Hypium 回放证据（请求体 `{"attempts": 1..3}`）；`POST /api/profiles/{id}/lock` 切换自动覆盖锁；`POST /api/profiles/{id}/rollback` 从校验过的历史版本原子回退。名称消歧使用 `POST /api/runs/{run_id}/target-selection`，仅接受该 Run 已返回候选集合中的精确 `bundle_name`。DC 会话蒸馏 Profile 使用 `POST /api/dc/sessions/{session_id}/profile/distill`。
