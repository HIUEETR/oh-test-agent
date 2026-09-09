# OpenHarmony 多模态测试 Agent 重构检查表

更新日期：2026-09-09

## 阶段 0：基线保护与目录边界

- [x] 保留 `mvp_phase1`、旧知乎++ smoke 和 `artifacts/phase1`
- [x] legacy 只作为设备可行性探针，不继续添加新业务
- [x] `.gitignore` 排除 `.env`、IDE、缓存、构建、日志、SQLite 和运行产物
- [x] Git 只跟踪 `artifacts/README.md`，可复现 fixture 迁入 `tests/fixtures/legacy/zhihu-plus/`
- [x] 增加带路径校验、ShouldProcess 和 `-WhatIf` 的 `scripts/clean-runtime.ps1`
- [x] `TargetAppProfile` 与动态 Observation/Run Trace 分离

## 阶段 1：工程骨架、配置与预检

- [x] 建立 `src/harmony_test_agent` 正式包
- [x] CLI 支持 `preflight`、`run`、`generate`、`execute`、`serve`
- [x] Python 3.14，固定 `hypium==6.1.0.210`、`pydantic-ai==1.73.0`
- [x] `.env.example`、配置脱敏和 Provider auto/openai/mock
- [x] 模型超时与设备动作超时分离
- [x] Hypium 子进程隔离 `HOME`、`USERPROFILE`
- [x] `uv lock --check` 通过，174 packages
- [x] 完整 preflight 通过，Key 未进入日志

## 阶段 2：HDC Adapter 与本地感知

- [x] 类型化 HDC Adapter 与设备断开错误
- [x] 设备端截图、`hdc file recv`、Pillow 解码、PNG、SHA-256 和分辨率
- [x] UI 层级保存、bounds 解析和系统节点过滤
- [x] 运行时 `element_id` 精确定位
- [x] 语义目标变体和稳定定位器优先级
- [x] 当前设备截图/布局联合验证：1320×2232、56 个元素
- [x] Hypium Driver 连接、启动、点击、输入、返回、断言和关闭

## 阶段 3：多模态模型与元素融合

- [x] OpenAI-compatible Pydantic AI Provider
- [x] 实际 PNG 二进制随 VLM 请求发送
- [x] `VisionObservation`、`PlanResult`、`ToolDecision` 结构化输出
- [x] thinking 模式兼容开关
- [x] ToolDecision 使用 PromptedOutput，降低兼容端点输出不稳定
- [x] UI 层级优先、VLM bbox 置信度/面积/边界校验
- [x] 无 Key 时确定性 Mock，不伪装成真实多模态
- [x] 真实模型 smoke：`deepseek-v4-flash-vision-exp`，`mock=false`
- [ ] OCR/OmniParser 插件（增强项，不阻塞主链路）

## 阶段 4：Agent 规划、执行与 Run Trace

- [x] 单工具白名单，无通用 Shell
- [x] 状态机、停止、有限重试、无变化检测和失败即停
- [x] 模型提前 finish 门禁
- [x] 规划严格对齐用户任务，不擅自提交搜索
- [x] 文本输入后的软键盘/页面返回处理
- [x] 安全策略只检查可执行 target/text，避免 reasoning 误报
- [x] 语义断言与 VLM 页面摘要辅助
- [x] 每个 Action 保存前后 Snapshot、命令、定位器、耗时和断言
- [x] 最终真实 Run 完整执行自然语言验收任务

## 阶段 5：页面图与 Hypium 生成/回放

- [x] 页面签名、节点、动作边和事件
- [x] 从成功 Run Trace 生成固定模板 Python、JSON 和 SHA-256 元数据
- [x] 动态长数字 key 转为 `STARTS_WITH`
- [x] 运行时 spatial 元素从 before Snapshot 确定性转换为坐标
- [x] 坐标降级写入代码注释、JSON、元数据、报告和 Web 告警
- [x] Runner 保存命令、环境、返回码、stdout/stderr、结果和截图
- [x] 每次回放前按 Profile stop/start 重置应用
- [x] 生成用例连续 3 次成功
- [x] Web/API 触发连续 3 次回放成功
- [ ] DevEco Testing 测试工程模式（没有已验证模板，保持 `not_validated`）

## 阶段 6：FastAPI、SSE 与 Web

- [x] Health、Devices、Runs、Stop、Events、Graph、Script、Report、Generate、Execute、Artifact API
- [x] SSE 事件 ID 1—117 有序读取并在终止状态关闭
- [x] React/Vite/TypeScript 任务控制、实时截图、事件、元素和断言
- [x] React Flow 页面关系图
- [x] Hypium 代码、3 条生成告警和三次回放按钮
- [x] HTML 报告 iframe 与下载
- [x] 移除运行时外部字体 CDN，内联 favicon
- [x] Vite production build 通过
- [x] 真实浏览器 live/graph/script/report 检查通过
- [x] 1440px 与 375px 无横向溢出
- [x] 浏览器 console errors=0、request failures=0

## 阶段 7：回归、文档与冻结交付

- [x] Ruff 检查正式代码、legacy 和 smoke
- [x] Ruff format check 通过，55 Python files formatted
- [x] 自动测试：33 passed、1 skipped、1 个 Starlette 上游 deprecation warning
- [x] `uv lock --check`、Vite build、`git diff --check` 通过
- [x] 真实模型输出错误、thinking 冲突和 30 秒模型超时已在实测中暴露并修复
- [x] 元素缺失、断言失败、安全拒绝、坐标越界、失败即停有自动测试
- [x] HDC 真实截图、真实 Agent、真实 Hypium 和 Web/API 完整闭环
- [x] README、架构、Hypium 基线和详细启动手册
- [ ] 刻意断开物理/模拟设备的破坏性实测未执行；设备失败路径由 Adapter/集成测试覆盖
- [ ] 历史失败 Run 的不可恢复删除未执行；Git 已忽略，使用清理脚本前必须显式确认

## 2026-09-09 最终验收证据

### 预检

- Python 3.14.0
- Hypium 6.1.0.210
- 设备 `127.0.0.1:5555`
- 分辨率 1320×2232
- HDC screenshot transfer / Pillow decode / SHA-256 通过
- 56 个 UI 层级元素
- 模型和 VLM 配置完整，Key 仅记录为 `configured`

### 真实 Provider smoke

- 模型：`deepseek-v4-flash-vision-exp`
- `mock=false`
- 实际 1320×2232 PNG 输入
- 52 个 HDC 层级元素
- 结构化页面标题、摘要、元素和 ToolDecision 返回成功

### 最终 Run

- Run ID：`run-20260909T140205Z-e9ada52e`
- State：`completed`
- `model_mock=false`
- 18 Actions
- 19 Snapshots，所有 Action 均有 before/after
- 2 Assertions，均通过
- 19 页面/状态节点
- 17 动作边
- 实际发生 open、click、input、return、detail、assert 和 finish

### Hypium

- attempt-01：returncode 0，passed true，约 29.2 秒
- attempt-02：returncode 0，passed true，约 26.3 秒
- attempt-03：returncode 0，passed true，约 26.9 秒
- 每次均保存 command/environment/stdout/stderr/result/final.jpeg
- Web 按钮再次触发 3 次，API 200，全部成功

### API/SSE/Web

- `/api/health`：模型、设备、Hypium 均正常
- `/api/devices`：1 台在线设备
- Graph：19 nodes / 17 edges
- Script：3 warnings
- Report、PNG Artifact：HTTP 200
- SSE：117 events，first=1，last=117，ordered=true
- 浏览器：live/graph/script/report 正常
- 375px：scrollWidth=375
- 1440px：scrollWidth=1440
- console errors=0，request failures=0

## 仍需后续决策

- 是否提供 DevEco Testing 工程模板并完成测试工程模式。
- 是否增加 OCR/OmniParser 插件。
- 是否对动态页面状态进行语义聚类，减少页面图过度分裂。
- 是否为 FastAPI 增加鉴权、任务队列和多设备并发隔离。
