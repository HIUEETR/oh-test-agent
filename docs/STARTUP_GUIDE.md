# OpenHarmony 多模态测试 Agent 启动手册

更新日期：2026-09-10

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
→ 连续 3 次真机回放
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
  runner/                     Hypium 子进程与三次回放
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
| `AGENT_DISABLE_THINKING` | 某些兼容端点的 thinking 模式与结构化工具输出冲突时设为`true`。                     |
| `HDC_PATH`               | `hdc.exe` 的绝对路径；若 HDC 已在 `PATH` 中可留空。                              |
| `HARMONY_DEVICE`         | 设备序列号，默认`127.0.0.1:5555`。                                                 |
| `AGENT_ACTION_TIMEOUT`   | HDC/设备动作超时，默认 30 秒。                                                       |
| `AGENT_MODEL_TIMEOUT`    | 规划、VLM 和工具决策超时，默认 90 秒。不要与设备动作超时混用。                       |
| `AGENT_RETRY_LIMIT`      | 瞬时动作失败的有限重试次数；断言和元素缺失默认立即停。                               |
| `UNCHANGED_SCREEN_LIMIT` | 连续可变动作截图不变化的停止阈值，默认 2。                                           |
| `VLM_MIN_CONFIDENCE`     | VLM 视觉元素进入融合列表的最低置信度，默认 0.55。                                    |
| `RUNTIME_HOME`           | Hypium/xdevice 的项目内隔离 Home，默认`.runtime-user`。                            |

### 5.2 thinking 兼容性

若真实模型返回：

```text
Thinking mode does not support this tool_choice
```

设置：

```dotenv
AGENT_DISABLE_THINKING=true
```

系统只会向模型请求增加兼容端点的 `extra_body.thinking.type=disabled`，不会修改设备工具或安全策略。2026-09-09 的 DeepSeek 兼容端点验收使用了该设置。

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

默认会在 Agent 成功后生成 Hypium Python、JSON 与元数据。加 `--no-generate` 可只执行 Agent；加 `--execute` 会在同一命令中生成并回放 3 次。

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

### 10.5 Web 功能

Web 控制台支持：

- 创建自然语言 Run；
- 实时设备截图、元素、事件和断言；
- 停止正在运行的任务；
- React Flow 页面关系图；
- Hypium Python、生成告警和配置；
- 通过按钮重新生成并连续回放 3 次；
- 内嵌 HTML 报告与下载。

可用深链接直接查看 Run：

```text
http://127.0.0.1:5173/?run_id=<run-id>&tab=live
http://127.0.0.1:5173/?run_id=<run-id>&tab=graph
http://127.0.0.1:5173/?run_id=<run-id>&tab=script
http://127.0.0.1:5173/?run_id=<run-id>&tab=report
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
```

`GET /api/health/live` 是轻量进程存活探针，不访问设备或外部模型，适合启动器和自动化轮询。`GET /api/health` 返回模型配置、设备连通性和 Hypium 状态，检查范围更完整，响应时间也可能受 HDC 影响。

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/health/live'
Invoke-RestMethod 'http://127.0.0.1:8000/api/health'
```

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

设置 `AGENT_DISABLE_THINKING=true`，然后重启 CLI/API。

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

## 16. 当前限制

- 不处理登录、支付、验证码、删除、卸载、清除数据和授权类动作。
- 默认演示依赖知乎++已安装，核心接口本身应用无关。
- VLM bbox 必须通过置信度、面积和边界检查；无法确认时会失败而非盲点。
- 信息流和内容卡片是动态数据，回放使用 key 前缀或受告警坐标。
- Hypium Driver 模式已验证；DevEco Testing 测试工程模式仍为 `not_validated`。
- API 目前面向本机开发，没有鉴权，不应直接暴露到不受信任网络。

## 17. 2026-09-09 最终验收记录

- 预检：Python 3.14.0、Hypium 6.1.0.210、设备 `127.0.0.1:5555`、1320×2232、截图传输和 56 个 UI 元素全部通过。
- 真实 Provider smoke：实际 PNG 发送到 `deepseek-v4-flash-vision-exp`，`mock=false`，返回结构化页面理解和工具决策。
- 最终 Run：`run-20260909T140205Z-e9ada52e`，`completed`、`model_mock=false`、18 个动作、19 张 Snapshot、2 个通过断言、19 个页面状态、17 条边。
- Hypium：3 次回放均 `returncode=0`、`passed=true`；最终保存的耗时约为 29.2 秒、26.3 秒、26.9 秒。
- Web：真实浏览器检查 live/graph/script/report，页面图 19 节点、脚本 3 条告警、报告 iframe 正常；1440px 和 375px 均无横向溢出，浏览器控制台和请求失败为 0。
- Web 回放：浏览器按钮触发 `POST /execute?attempts=3`，API 返回 200，3 次均成功。
- 自动门禁：33 passed、1 skipped（显式 live 测试默认跳过）；Ruff、uv lock、Vite build、`git diff --check` 全部通过。
