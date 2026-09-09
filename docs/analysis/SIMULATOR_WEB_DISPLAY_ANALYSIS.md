# 模拟器画面在 Web 中展示的方案分析

## 结论

当前 Web 已能显示 Agent 最近一次落盘截图，但它展示的是离散采样结果，不是模拟器实时画面。后端通过 HDC 在设备端截图、拉回 PNG、同时采集 UI hierarchy，并把文件路径写入 `ScreenSnapshot`；前端每 800 ms 轮询完整 Run Trace，从最后一个 Snapshot 组装 artifact URL 并显示图片。对应实现见 `src/harmony_test_agent/devices/harmony.py:105-143`、`src/harmony_test_agent/agents/orchestrator.py:130-138`、`web/src/App.tsx:74-85`、`web/src/App.tsx:152-154`、`web/src/App.tsx:211-215`。这条链路适合测试证据和步骤回放，也符合任务书对“当前截图”和 SSE 日志的 P0 要求（`docs/PROJECT_REQUIREMENTS.md:293-304`），但动作之间没有持续帧，无法表现动画、加载、短暂弹窗或触摸反馈。

本项目应保留“证据截图”作为事实源，并在此基础上增加“按需预览帧”。第一阶段使用 HDC 周期截图构建 1–2 FPS 的低频预览，第二阶段仅在确有演示需求且设备能力验证通过后接入视频流。不要用浏览器直接访问 HDC，也不要把预览帧替代 Run Trace 中带 SHA-256、布局和动作关联的证据截图。

## 当前静态实现

`HarmonyDeviceAdapter.screenshot()` 调用设备截图、`hdc file recv`、Pillow 解码和 PNG 保存，然后调用 `collect_ui_hierarchy()`，将布局写入同一 Run 的 `layouts/`，最后生成包含图像哈希、尺寸、布局路径和标准化元素的 `ScreenSnapshot`（`src/harmony_test_agent/devices/harmony.py:105-143`）。截图失败会重试并要求文件能被 Pillow 解码（`src/harmony_test_agent/devices/harmony.py:145-171`）。因此当前画面具备审计价值，代价是每帧至少包含截图、文件传输、图像解码、dumpLayout 和 JSON 解析，不适合无节制高频调用。

每轮执行只在需要当前状态时采集 before Snapshot，并在动作后采集 after Snapshot；执行器等待页面稳定后再采集（`src/harmony_test_agent/agents/orchestrator.py:130-138`、`src/harmony_test_agent/agents/orchestrator.py:190-195`）。截图事件只携带 snapshot ID、路径、尺寸和 SHA-256（`src/harmony_test_agent/agents/orchestrator.py:296-305`）。SSE 后端每 500 ms 查询 SQLite 新事件（`src/harmony_test_agent/api/app.py:147-167`），而前端收到事件后只追加事件列表；画面更新仍依赖完整 Trace 轮询（`web/src/App.tsx:52-85`）。这会重复传输 snapshots、actions、graph 和 events，Run 越长负载越大。

前端使用 artifact endpoint 显示最后一张截图（`web/src/App.tsx:263-270`）。图片被限制在设备卡片内并保持纵横比（`web/src/styles.css:92-93`），没有帧时间、陈旧状态、缩放坐标或标注层。`Snapshot` 的 TypeScript 类型也没有暴露 `bbox` 和 `image_sha256`（`web/src/types.ts:20-36`），因此现阶段不能在画面上准确叠加元素框或点击点。

现有文档声称 2026-09-09 已完成 Web 真浏览器和真实设备验收（`README.md:178-183`，`docs/ARCHITECTURE.md:272-283`）。这些属于仓库中的历史声明；本次受约束未启动服务、模拟器或 HDC，无法复验当前 checkout 的运行时延迟、帧率、跨视口布局和设备连接。

## 方案比较

| 方案 | 画面时效 | 证据能力 | 后端与设备负载 | 主要改动 | 适用结论 |
| --- | --- | --- | --- | --- | --- |
| 现有动作截图 + Trace 轮询 | 动作级，通常秒级 | 最强，截图与布局、动作、哈希关联 | 中；轮询完整 Trace 会随 Run 增长 | 无 | 保留为审计基线，不足以称为实时画面 |
| SSE 推送新 Snapshot 元数据 | 动作级，事件到达即更新 | 与现有证据相同 | 低；避免 800 ms 全量轮询 | 扩展事件 payload 和前端 reducer | 必做的短期优化 |
| HDC 周期截图，JPEG/WebP 最新帧 | 0.5–2 FPS | 预览帧弱，关键帧另行落盘 | 中高；频率必须限流，且预览不应每次 dumpLayout | 独立 preview worker、最新帧槽、帧 API/SSE | 推荐演示方案 |
| HDC/设备视频编码流转 WebSocket/MJPEG | 10–30 FPS | 视频本身难与动作和 hierarchy 精确关联 | 高；受设备命令、编码器、网络和浏览器支持影响 | 流进程、转码、断线恢复、背压 | 截止日前不优先，需先实机验证能力 |
| DevEco 模拟器窗口采集 | 近实时 | 只能证明宿主窗口像素，难关联设备状态 | 中；依赖 Windows 桌面和窗口权限 | 桌面捕获、裁剪、窗口定位 | 不进入服务架构，仅可用于录屏材料 |

## 推荐架构

后端将“证据采集”和“画面预览”分成两个明确通道。证据通道继续调用现有 `screenshot()`，同时保存 PNG、hierarchy、SHA-256 并写入 Run Trace。预览通道增加 `capture_frame()`，只截图和传输，不执行 `dumpLayout`；同一设备最多一个 producer，内存中仅保存最新帧，旧帧直接丢弃。这样可以避免预览阻塞 Agent 的设备操作队列。

所有 HDC 操作仍需串行。当前任务书规定设备操作不能并行（`docs/PROJECT_REQUIREMENTS.md:249-258`），所以预览 worker 在 Agent 操作前暂停，在动作完成和 settle 后恢复。producer 使用设备 ID 作为互斥键，设置 500–2000 ms 可调间隔和 1 个帧的有界缓冲。前端掉线或没有订阅者时停止截图；运行终止后延迟数秒关闭 producer。

建议新增如下只读接口；其中 URL 仅为实施约定，未在当前代码中存在：

```http
GET /api/devices/{device_id}/preview/frame
ETag: "<frame-sha256>"
Cache-Control: no-store
Content-Type: image/jpeg
X-Frame-Sequence: 184
X-Captured-At: 2026-09-10T10:15:30.482Z
X-Device-Width: 1320
X-Device-Height: 2232
```

事件流增加 `preview_frame` 或扩充 `screen_captured` payload，使前端按序列号更新 URL，而不轮询完整 Trace：

```json
{
  "device_id": "127.0.0.1:5555",
  "sequence": 184,
  "captured_at": "2026-09-10T10:15:30.482Z",
  "width": 1320,
  "height": 2232,
  "frame_url": "/api/devices/127.0.0.1%3A5555/preview/frame?v=184",
  "kind": "preview"
}
```

前端状态应分别维护 `latestEvidenceSnapshot` 和 `latestPreviewFrame`。设备卡默认显示预览；用户暂停或 Run 结束后固定到最新证据截图。画面旁显示“实时预览 / 证据截图”、采集时间、帧序号和距今时间。超过两个帧周期没有更新时标记“画面已暂停”，不得继续显示成“实时”。

## 实施级计划

第一步修改 `HarmonyDeviceAdapter`，抽出当前截图和 file recv 的公共私有方法，新增不采 hierarchy 的 `capture_frame()`。返回值至少包含 bytes 或临时路径、设备像素尺寸、SHA-256、采集时间和耗时。保留现有 `screenshot()` 行为，避免改变 Run Trace 与测试生成链路。

第二步增加设备级 `PreviewManager`。其职责是订阅计数、设备互斥、频率限制、最新帧原子替换、序列号、错误状态和停止清理。使用容量为 1 的最新值模型；不积压帧，不把所有预览写进 SQLite，也不写入 `artifacts/runs/`。指标至少记录 capture latency、transfer bytes、dropped frames、active subscribers 和 error count。

第三步扩展 API。新增启停订阅和取最新帧接口，返回 `ETag` 与 `no-store`。SSE 只发送元数据，不嵌入 base64 图片；图片仍由独立 endpoint 获取，减少 SQLite 事件体和 Trace 大小。为防止路径和设备 ID 注入，API 只接受已注册设备 ID，文件响应不得使用客户端提交的磁盘路径。

第四步调整前端。以 SSE 驱动画面刷新，终止对完整 Trace 的 800 ms 连续轮询；仅在关键事件或 2–5 秒低频拉取一次 Trace。增加预览状态、错误提示、暂停按钮和证据截图切换。随后补齐 `Snapshot.bbox`、缩放比例和 overlay 坐标映射，为视觉定位文档中的标注层预留能力。

第五步补充验证。单元测试验证 producer 单例、容量 1 丢帧、订阅归零停止和 ETag；API 测试验证帧 MIME、序列号、非法设备 ID 和无帧状态；前端测试验证乱序帧不会覆盖新帧、超时显示暂停、Run 终止切换到证据截图。真实验收必须另行在模拟器上记录 60 秒帧率、P50/P95 延迟、HDC 错误率、CPU 与内存，并检查 Agent 点击期间没有并发 HDC 命令。

视频流方案的进入条件是低频预览仍不能满足演示、目标 OpenHarmony 镜像存在稳定的视频抓取能力、连续 10 分钟无断流且能在 Agent 操作时正确互斥。满足后再在 `PreviewManager` 下增加另一种 producer，前端协议保持不变。

## 验收口径与风险

静态实现可确认现有截图可落盘、作为 artifact 返回并由 Web 显示；自动测试可确认 API 和前端状态机；只有模拟器实测才能确认画面时效、资源消耗、断线恢复和操作互斥。任务书要求前端实时显示任务进度、截图、日志、脚本和页面图（`docs/PROJECT_REQUIREMENTS.md:89-101`），因此“实时”验收应量化为：预览启用时 P95 端到端延迟不高于 2 秒，动作证据截图在事件后 1 秒内显示，断流在 2 个周期内显式提示，且每个证据截图仍保留原图、hierarchy 和 SHA-256。

主要风险来自 HDC 截图成本、预览与动作竞争、SSE 断线不自动补帧、浏览器缓存旧图片和 Run 结束后的 worker 泄漏。通过设备互斥、单帧缓冲、序列号、`no-store`、订阅引用计数和终止清理控制。若真实设备测得截图耗时长期超过周期，系统应自动降低 FPS，不能通过并发截图追赶。
