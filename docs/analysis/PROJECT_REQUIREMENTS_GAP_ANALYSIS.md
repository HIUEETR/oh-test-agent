# 项目任务书实现差距分析

## 分析口径

本文以当前 checkout 的代码、配置、测试和文档为证据，逐项对照 `docs/PROJECT_REQUIREMENTS.md`。状态分为四类：“静态已实现”表示当前代码存在可追踪链路；“自动测试覆盖”表示仓库测试可验证部分行为；“历史声明已验证”表示 README 或验收文档记载过真实结果；“当前无法复验”表示本次遵守约束，没有启动项目、服务或模拟器，也没有调用 HDC、真实模型或 Hypium。

任务书本身仍写着“项目实施尚未开始”（`docs/PROJECT_REQUIREMENTS.md:3-8`），且计划表全部保持未勾选；当前仓库代码和后续文档明显晚于该状态。本文不修改原任务书，也不把计划表中的 `[ ]` 自动改成完成。历史文档声称 2026-09-09 完成真实模型、真实设备、Hypium 三次回放和 Web 全链路（`README.md:3-5`、`README.md:178-183`），这些声明可作为历史证据，不能替代当前环境复验。

## P0 对照

任务书 P0 位于 `docs/PROJECT_REQUIREMENTS.md:227-304`。

| P0 要求 | 当前证据 | 判断与差距 |
| --- | --- | --- |
| Python、uv、ruff、Node.js、npm 版本检查 | `pyproject.toml` 固定 Python 与开发工具依赖；preflight 只检查 Python 版本和 Python 模块（`src/harmony_test_agent/preflight.py:49-58`） | **部分实现**。uv、ruff、Node.js 和 npm 的运行时版本检查未实现 |
| 模型 API 配置检查 | `Settings.model_configured`、`vision_model_configured`，健康 API 返回 configured 状态（`src/harmony_test_agent/config.py:63-71`、`src/harmony_test_agent/api/app.py:94-103`） | 静态已实现；真实 endpoint 当前未复验 |
| HDC 发现、连接、设备断开明确失败 | Adapter 检查 `list targets`，Orchestrator 预检失败进入 `failed_device`（`src/harmony_test_agent/devices/harmony.py:85-103`、`src/harmony_test_agent/agents/orchestrator.py:94-99`） | 静态已实现，有 mock/错误路径测试；真实断开破坏性实测历史文档明确未做（`docs/REFACTOR_CHECKLIST.md:96-97`） |
| Hypium Driver smoke、项目 smoke、Runner 报告 | Driver Runner 和三次执行实现；历史文档声明 Driver 三次回放成功 | **项目 smoke 未实现/未验证**。仓库明确保持 DevEco Testing 测试工程模式 `not_validated`（`docs/REFACTOR_CHECKLIST.md:69-70`、`docs/hypium-baseline.md:59-71`） |
| TargetAppProfile 加载、安装、启动、重置检查 | Profile 加载和 stop/start reset 存在；`profiles/zhihu-plus.json:2-27` 有应用标识、设备和 reset strategy | 部分实现。代码主链路没有安装目标应用步骤；安装能力与重置效果当前未复验 |
| 截图、时间、UI hierarchy、统一 ID、元素属性 | Snapshot 模型和 Adapter 保存 PNG、布局、元素；normalizer 生成统一 ID（`src/harmony_test_agent/devices/harmony.py:121-143`、`src/harmony_test_agent/perception/normalizer.py:58-114`） | 静态已实现大部分 |
| OCR 识别可见文本 | 未找到 OCR 引擎、OCR provider 或 OCR 结果模型 | **未实现**；当前文本来自 hierarchy 或 VLM，不是独立 OCR |
| OmniParser/视觉解析器补充图标和 bbox | OpenAI-compatible VLM 返回 `VisionObservation` 并融合（`src/harmony_test_agent/agents/providers.py:285-309`、`src/harmony_test_agent/perception/service.py:16-53`） | 以 VLM 替代实现；真实模型当前未复验 |
| 可视化标注截图 | Web 只显示原截图和元素列表（`web/src/App.tsx:211-236`）；类型未暴露 bbox（`web/src/types.ts:20-36`） | **未实现** |
| 自然语言拆成原子步骤、串行工具 | Provider 结构化 plan，Orchestrator 顺序执行（`src/harmony_test_agent/agents/providers.py:268-283`、`src/harmony_test_agent/agents/orchestrator.py:101-156`） | 静态已实现；真实模型当前未复验 |
| 每动作前获取当前状态 | 首步采集 before，动作后采集 after 并作为下一步 current Snapshot（`src/harmony_test_agent/agents/orchestrator.py:130-138`、`src/harmony_test_agent/agents/orchestrator.py:178-222`） | 静态满足顺序状态更新 |
| 元素不存在有限重试 | `_execute_with_retry()` 将 `FAILED_ELEMENT` 定为 non-retryable（`src/harmony_test_agent/agents/orchestrator.py:317-336`） | **与任务书不一致**。设备动作可有限重试，元素缺失不重试，也没有重新采集后再定位 |
| 等待、关键词等待、操作后截图 | 固定秒数 wait 和操作后截图存在 | **关键词等待未实现**；固定 wait 已实现 |
| 失败即停、手动停止 | 异常落失败状态；RunManager/stop endpoint 可停止（`src/harmony_test_agent/api/app.py:53-58`、`src/harmony_test_agent/api/app.py:140-145`） | 静态已实现；停止只能在步骤边界观察，不能强制中断正在执行的阻塞调用 |
| 点击、输入、滑动、返回、文本断言 | `ToolExecutor` 与设备 Adapter 均有实现 | 静态已实现并有单元/集成测试 |
| Tab/频道切换、弹窗确认或关闭 | 没有专用 tool，但可通过通用 `click_element` 表达 | 能力层面可覆盖；缺少专门场景测试与当前真实证据，不能判为稳定完成 |
| 页面不包含文本断言 | `ASSERT_NOT_VISIBLE` 已建模和执行（`src/harmony_test_agent/models.py:81-95`、`src/harmony_test_agent/runtime/tools.py:146-185`） | 静态已实现 |
| Hypium Python、JSON、元数据 | 生成器输出三个文件（`src/harmony_test_agent/generation/hypium.py:19-69`） | 静态已实现 Driver 模式 |
| 用例名称、目的、setup/process/teardown、步骤说明 | 当前模板是单个 `main()`，按动作输出代码（`src/harmony_test_agent/generation/hypium.py:172-221`） | **未严格满足**。没有任务书定义的 setup/process/teardown 结构，也没有独立测试目的字段和逐步说明元数据 |
| 生成前后 Python 语法检查 | 生成器直接写文件，未见 `ast.parse`、`compile` 或生成后 lint | **未实现** |
| 坐标降级 `stability=degraded` | 有 warning 和 coordinate count（`src/harmony_test_agent/generation/hypium.py:44-63`） | **部分实现**。缺少结构化 `stability=degraded`；`VLM_BBOX` 还会漏告警并错误退化为文本定位 |
| 页面稳定签名、去重、节点和边 | `PageGraphBuilder` 生成 SHA-256 签名、严格相等去重和边（`src/harmony_test_agent/graph/service.py:20-87`） | 静态已实现；动态内容可能过度分裂，历史文档也列为已知问题 |
| 图中节点截图、点击节点查看元素和记录 | PageNode 保存 image path；React Flow 节点只显示标题和元素数量（`web/src/App.tsx:273-286`） | **部分实现**。图页没有节点截图，也没有点击详情 |
| Web 任务、健康、状态、SSE、截图、元素、脚本、图、报告 | `web/src/App.tsx:157-256` 覆盖主要视图 | 静态已实现大部分；脚本是生成完成后整体读取，没有流式/分段生成 |
| 失败、停止、重新运行 | 有失败展示和停止；启动按钮可新建 Run | **部分实现**。没有明确“按历史 Run 一键重新运行”的入口 |

## P1 对照

任务书 P1 位于 `docs/PROJECT_REQUIREMENTS.md:306-317`。

| P1 要求 | 当前证据 | 判断与差距 |
| --- | --- | --- |
| 第二个合格目标应用 | 前端和默认配置固定 `zhihu-plus`（`web/src/App.tsx:102-106`、`web/src/App.tsx:179-180`） | **未实现** |
| 探索性测试模式 | 枚举、下拉框和请求 mode 存在（`src/harmony_test_agent/models.py:18-25`、`web/src/App.tsx:175-178`） | **仅接口层**。Orchestrator 没有按 mode 实施自动探索、最大节点、已尝试元素策略 |
| 已有序列 N 次重复 | API 只对生成 Hypium 支持 1–3 attempts；前端固定 3 次（`web/src/App.tsx:136-145`） | 部分实现回放稳定性；没有通用 N 次运行统计 |
| 崩溃、超时、白屏、无变化检测 | 模型/动作超时和 screenshot SHA 无变化阈值存在（`src/harmony_test_agent/config.py:32-36`、`src/harmony_test_agent/agents/orchestrator.py:211-221`） | 部分实现。没有崩溃日志判定、白屏图像判定和 UI 无响应专门分类 |
| 自然语言问题复现模式 | mode 枚举存在 | **仅标签**。未解析“步骤/预期”结构，也没有 reproduction 专属行为 |
| 成功轨迹保存为回归用例 | Run Trace 持久化，可生成脚本 | 部分实现；没有用例目录、命名、版本和提升为回归集的工作流 |
| 用例重新加载和执行 | 可按 run_id 从数据库取 Trace，并对已生成文件执行 | 部分实现；前端没有运行列表/选择历史用例/重新执行闭环 |
| 运行结果摘要 | API list_runs 返回摘要；HTML 报告有状态与计数 | 静态已实现基础摘要，缺稳定性聚合统计 |
| Markdown、JSON 或 HTML 报告 | `ReportBuilder` 生成 HTML 和 JSON（`src/harmony_test_agent/reporting.py:19-43`） | 静态已实现 |
| 模型供应商切换 | `agent_provider` 只有 auto/openai/mock；OpenAI-compatible base URL 可换 | 部分实现。兼容 endpoint 可切换，未实现多个命名供应商配置与运行时选择 |

## 最终交付物对照

任务书交付物位于 `docs/PROJECT_REQUIREMENTS.md:1054-1092`。

| 交付物 | 当前状态 | 缺口 |
| --- | --- | --- |
| FastAPI 后端、React/Vite/TypeScript 前端 | 静态存在 | 当前未启动复验 |
| 已准入目标应用与完整 Profile | 有 `profiles/zhihu-plus.json`，历史文档声明已验证 | 目标应用源码/安装包不在交付清单中可见；当前安装、启动、重置未复验 |
| HDC Adapter、UI 感知、Pydantic AI Agent、页面图 | 静态存在 | OCR、标注图、图节点详情缺失 |
| Hypium 测试工程生成器 | Driver 脚本/JSON生成存在 | **DevEco Testing 项目 smoke 和测试工程模式未完成**；生成结构未严格匹配 setup/process/teardown |
| Hypium 基线、Driver smoke、项目 smoke、回放证据 | 文档有 Driver 历史证据 | **项目 smoke 缺失**；运行产物被 Git 忽略，当前 checkout 未提供可复验动态证据 |
| 报告生成器、测试和 Mock 数据 | 静态存在 | 报告未汇总 token、缓存、稳定性指标；失败截图依赖 Runner 实际执行 |
| lockfile、Ruff 配置 | `uv.lock`、`web/package-lock.json`、Ruff 配置存在 | 静态满足 |
| 本机一键启动说明 | `docs/STARTUP_GUIDE.md` 有分步命令 | 没有单一一键启动脚本；“说明”可视作部分满足 |
| 架构设计文档 | `docs/ARCHITECTURE.md` 存在 | 静态满足 |
| 测试用例生成说明 | 分散在架构、启动、Hypium baseline | **缺少独立交付文档** |
| Hypium 运行说明、环境安装说明 | `docs/STARTUP_GUIDE.md`、`docs/hypium-baseline.md` | 静态存在 |
| API 说明 | 启动指南有 API 与 SSE 章节 | **缺少完整 API 参数、响应、错误和 curl/JS/Python 示例的独立文档** |
| 验证报告 | README、checklist、baseline 记录历史结果 | **缺少明确版本化的最终验证报告及可携带证据索引** |
| 已知问题和限制 | README/架构有章节 | 存在但分散 |
| 演示脚本 | 未找到独立演示脚本文档 | **未交付** |
| 后续产业化规划 | 未找到独立规划 | **未交付** |
| 完整流程录屏 | 仓库无可见录屏 | **未交付或未纳入仓库** |
| 页面关系图演示材料 | 运行时可生成 | **无静态导出材料** |
| 生成工程文件与执行成功报告 | 运行时目录被忽略 | 历史声明存在，当前 checkout 无可直接检查的样例交付包 |
| 准入记录、Runner 命令、报告目录说明 | Profile、baseline、startup guide 有相关内容 | 分散；可整理成提交包 |
| 系统架构图和数据流图 | 架构文档有文本图和流程 | 静态存在 |

## 最终验收逐项判断

最终验收清单位于 `docs/PROJECT_REQUIREMENTS.md:1096-1118`。下表严格保留任务书口径；“历史通过”只引用仓库声明，本次不重复运行。

| 最终验收项 | 静态实现 | 历史声明 | 当前判断 |
| --- | --- | --- | --- |
| 目标应用通过准入且 Profile 完整 | Profile 存在 | baseline 声明知乎++已验证 | 当前无法复验准入全部条件 |
| 应用可安装、启动、重置 | start/stop reset 有实现；无安装主链路 | 历史声明可运行 | 安装能力证据不足，当前无法复验 |
| 模拟器被 HDC 发现 | Adapter 有实现 | README 声明通过 | 当前无法复验 |
| Hypium Driver smoke | Runner/基线存在 | 历史通过 | 当前无法复验 |
| Hypium 项目 smoke 生成报告 | 无完成实现 | 文档明确 `not_validated` | **未通过** |
| 至少识别 3 类控件/交互 | hierarchy/VLM 模型可表示多类 | 历史 52/56 元素 | 静态可支持，当前无法复验稳定识别 |
| 点击、输入、滑动、返回、断言 | 有代码和自动测试 | 历史真实链路通过 | 静态满足，当前无法复验设备行为 |
| 自然语言拆解顺序步骤 | 有 Provider 和 mock 测试 | 历史真实模型通过 | 静态满足，当前无法复验真实模型 |
| 至少 3 页面或核心流程 | 图模型支持 | 历史声称完整 Run | 当前 artifacts 不在版本库，无法核对当前三页证据 |
| 页面图 3 节点 2 边 | Builder 支持 | 历史 Web 图通过 | 当前无法核对具体 Run |
| 脚本有步骤和断言 | 动作模板与 assertion 生成存在 | 历史回放通过 | 部分满足；缺任务书要求的完整用例说明结构 |
| 生成工程由 Runner 实际执行 | Runner 存在 | Driver 三次通过 | Driver 模式历史通过；项目模式未通过 |
| 保存 exit code、报告、stdout、stderr、失败截图 | Runner 静态实现 | 历史成功报告声明 | 失败截图仅失败场景可证；当前无法复验 |
| 主流程连续成功 3 次 | execute_repeated 存在 | README 声明三次成功 | 当前无法复验 |
| 回归和探索两类场景 | mode 枚举存在 | 未找到探索策略和独立探索验收证据 | **未通过/证据不足** |
| 前端实时展示过程 | SSE + 轮询 + 四个 tab | 历史浏览器通过 | 静态满足主要功能；画面为动作截图，当前无法复验时效 |
| 报告含截图、日志、步骤和结果 | HTML 含动作、断言、截图；Trace JSON 含更多 | 历史报告声明 | **部分满足**：HTML 未直接列出完整日志/命令 stdout/stderr |
| 失败后不继续危险步骤 | 异常结束主循环，SafetyPolicy 存在 | 自动错误测试历史通过 | 静态与自动测试可支持 |
| 架构、生成说明、验证报告、演示材料齐全 | 架构与分散说明存在 | 历史文档有验收摘要 | **未通过**：独立生成说明、最终验证报告、演示脚本、录屏等不齐 |

## 完成定义闭环

任务书完成定义要求第三方按 README 和演示脚本重复执行“自然语言任务 → 模拟器 → Profile → 截图/元素 → 规划/执行 → 三页页面图 → Hypium Python+JSON → Runner → 报告”（`docs/PROJECT_REQUIREMENTS.md:1137-1151`），并要求区分真实验证、静态实现、外部依赖和后续增强（`docs/PROJECT_REQUIREMENTS.md:1154-1159`）。

当前代码已经形成 Driver 模式的静态闭环，README 和历史验收文档声称曾完成真实闭环。但按任务书的字面完成定义，项目仍不能标记完成，原因包括：Hypium 项目 smoke 明确未验证；探索性场景只有 mode 标签；OCR 和标注截图未实现；P0 的关键词等待、元素缺失有限重试、生成前后语法检查和结构化 degraded 标记缺失；交付文档与演示材料不齐；本次无法复验被忽略的动态 artifacts 和外部设备/模型结果。

## 明确未实现或未达到任务书口径的项目

以下项目可由当前源码或文档直接判定，无需运行环境：

1. 独立 OCR 链路。
2. bbox/元素可视化标注截图。
3. 元素不存在后重新采集并有限重试。
4. 关键词等待。
5. Hypium DevEco Testing 项目 smoke 与测试工程模式。
6. 任务书定义的 setup/process/teardown 用例结构、测试目的和完整步骤元数据。
7. 生成前后 Python 语法检查。
8. 结构化 `stability=degraded`，以及 `VLM_BBOX` 到 Hypium 的正确坐标降级。
9. 页面图节点截图展示、节点点击后的元素与执行记录详情。
10. 脚本流式或分段生成展示。
11. 历史 Run 一键重新运行入口。
12. 第二个目标应用。
13. 真正的探索策略、问题复现解析和通用 N 次统计。
14. 崩溃与白屏专门检测。
15. 回归用例库、版本化保存、重载与执行工作流。
16. 独立测试用例生成说明、完整 API 文档、最终验证报告、演示脚本、产业化规划和完整流程录屏。

以下项目有静态实现或历史声明，但本次无法确认当前有效：HDC 设备发现、目标应用安装/启动/重置、真实模型规划和 VLM、三页页面图、Hypium 三次回放、真实浏览器四个 tab、移动/桌面视口以及报告中的动态运行证据。复验必须在允许启动服务与模拟器的验收轮次执行。

## 建议收口顺序

先处理会直接阻止最终验收的项目：完成 Hypium 项目 smoke 或与任务方书面确认 Driver 模式可替代；修复 `VLM_BBOX` 生成；实现探索性场景并形成第二类场景证据；补齐最终验证报告、演示脚本、录屏和可携带样例产物。随后补 P0 明确缺口：OCR、标注图、关键词等待、元素重试、语法检查和页面图详情。最后再做第二应用、崩溃/白屏分类、用例库与模型供应商运行时切换。

复验时应生成一个不可变验收索引，列出 commit、环境版本、Profile hash、run IDs、三次 Runner 目录、页面节点/边数量、报告路径、浏览器截图和录屏路径。只有全部最终验收项都有当前证据，才应更新任务书状态或宣布完成。
