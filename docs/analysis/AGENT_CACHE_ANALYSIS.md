# Agent 缓存现状与设计分析

## 结论

当前 Agent 没有模型结果缓存、视觉结果缓存、页面理解缓存或元素定位缓存。唯一明确使用 `functools.lru_cache` 的生产代码是 `get_settings()`，容量为 1（`src/harmony_test_agent/config.py:84-87`）。Run Trace 与事件写入 SQLite 和 JSON/PNG artifact 属于持久化，不具备命中、过期或淘汰语义；前端 React state 只是当前页面内存状态，也不构成可复用 Agent 缓存。

每次页面采集后都会再次调用 VLM，每个计划步骤都会再次调用模型决策。Provider 在 `plan()`、`analyze()` 和 `decide()` 中临时创建 Pydantic AI Agent 并发送请求（`src/harmony_test_agent/agents/providers.py:268-309`、`src/harmony_test_agent/agents/providers.py:311-345`）。Orchestrator 每次 capture 都执行分析并融合结果（`src/harmony_test_agent/agents/orchestrator.py:280-294`）。因此相同截图、相同模型配置和相同 prompt 重复出现时，当前实现不会复用结果。

应优先增加可观测性，再上线小范围、内容寻址、版本化的缓存。第一阶段只缓存 VLM screenshot analysis；规划结果可短 TTL 缓存；工具决策默认不跨步骤缓存，因为它依赖当前步骤、当前截图、当前候选元素、Profile 和运行状态，错误复用会直接产生设备动作风险。

## 当前缓存与持久化盘点

`get_settings()` 的 key 是零参数函数调用，效果等价于进程内常量。它没有 TTL，直到进程退出或显式 `cache_clear()` 才失效。测试或运行中修改 `.env` 后，同一进程不会自动读取新值。这个缓存应继续保留，但测试需要显式清理，运行时配置刷新需要重启进程或新增受控 reload。

`RunRepository` 将整个 `RunTrace` 序列化到 `runs.trace_json`，并按 `(run_id, event_id)` 保存事件（`src/harmony_test_agent/storage/repository.py:26-45`、`src/harmony_test_agent/storage/repository.py:47-100`）。`RunEventEmitter.emit()` 每个事件依次写事件、完整 SQLite Trace 和完整文件 Trace（`src/harmony_test_agent/runtime/events.py:26-38`）。这些数据支持恢复和审计，但没有 TTL、容量上限、GC 或 cache hit。`ArtifactStore` 为每个 Run 创建截图、布局、命令、生成、Hypium 和报告目录（`src/harmony_test_agent/storage/artifacts.py:21-37`），同样是永久产物，清理由外部脚本负责。

前端同时维护 SSE `events` 和轮询得到的 `trace`（`web/src/App.tsx:52-85`），可以避免同一 event ID 在 React state 中重复插入（`web/src/App.tsx:62-66`）。页面刷新后状态丢失；没有 Service Worker、IndexedDB 或 HTTP cache 策略。artifact 图片 URL 没有内容 hash query，是否缓存由浏览器和 FastAPI 默认头决定。

`web/package-lock.json` 出现的 `lru-cache` 是第三方依赖的传递包，并未被业务代码引用，不能作为项目已有缓存能力的证据。

## 缺失的基线指标

当前模型领域对象没有 token usage、模型 latency、provider request ID 或 cache status；`ActionResult` 只有设备动作 `duration_ms`（`src/harmony_test_agent/models.py:280-296`）。Run 报告只汇总页面、动作和断言数量（`src/harmony_test_agent/reporting.py:76-82`）。所以仓库无法回答“缓存能节省多少模型调用、token、耗时和费用”。

引入缓存前至少连续记录一组不改变行为的指标：

- `agent_model_requests_total{operation,model,outcome}`：`operation` 为 `plan`、`vision`、`decision`。
- `agent_model_latency_ms{operation,model}`：记录 P50、P95、P99。
- `agent_model_input_bytes{operation}`：视觉请求另记图像字节和 hierarchy 元素数量。
- `agent_model_tokens_total{operation,direction}`：Provider 能返回 usage 时记录 input/output tokens；不支持时标记 unknown，不填估算值。
- `agent_cache_requests_total{namespace,result}`：`result` 为 hit、miss、bypass、stale、error。
- `agent_cache_lookup_ms{namespace}`、`agent_cache_entry_bytes{namespace}`、`agent_cache_evictions_total{namespace,reason}`。
- `run_trace_write_bytes_total` 与 `run_trace_write_ms`：当前每事件全量保存 Trace，需独立观察写放大。

指标不得包含 API key、完整用户任务、截图原文或 hierarchy 文本。日志只记录 key 的短前缀、namespace、命中状态、模型标识和版本。

## 推荐缓存 namespaces、keys 与 TTL

所有 key 使用 canonical JSON 后的 SHA-256。canonical JSON 必须固定字段顺序、Unicode 编码和数值格式；任何会改变模型输出或安全语义的输入都进入 key。缓存 value 保留 schema version、created_at、expires_at、模型标识、prompt version 和原始结构化输出。

| Namespace | Key 输入 | 建议 TTL | 容量 | 是否允许跨 Run | 说明 |
| --- | --- | --- | --- | --- | --- |
| `vision:v1` | 图像 SHA-256、width/height、规范化 hierarchy 摘要、vision model、base URL 的非敏感 provider ID、`VISION_PROMPT_VERSION`、输出 schema version、`VLM_MIN_CONFIDENCE` | 24 小时；历史 fixture 可 7 天 | 512 MB 或 2,000 条 | 是 | 首选；相同像素和层级应得到可复用观察结果 |
| `plan:v1` | 规范化 task、Profile 语义摘要、model、provider ID、`PLANNING_PROMPT_VERSION`、max_steps、输出 schema version | 30 分钟 | 1,000 条 | 谨慎允许 | Profile 或 prompt 变化立即 miss；不得缓存安全拒绝以外的异常 |
| `decision:v1` | step JSON、图像 SHA-256、候选元素摘要、Profile stable locators、model、`DECISION_PROMPT_VERSION`、安全策略版本 | 默认禁用；启用时 2 分钟 | 200 条 | 否，key 中加入 run_id 与 step_id | 动作风险最高，只用于同一步超时重试去重 |
| `settings:v1` | 无显式 key | 进程生命周期 | 1 条 | 进程内 | 当前已有 `lru_cache(maxsize=1)` |
| `artifact-response:v1` | 文件内容 SHA-256 | immutable artifact 可长期 | 由浏览器控制 | 是 | 仅适合不可变证据文件；最新帧必须 `no-store` |

视觉 key 示例：

```text
vision:v1:sha256(
  image_sha256 + "\n" +
  hierarchy_digest + "\n" +
  model_id + "\n" +
  vision_prompt_version + "\n" +
  vision_schema_version + "\n" +
  "min_confidence=0.55"
)
```

`hierarchy_digest` 不应直接使用文件路径或运行时 `element_id`。当前 `element_id` 把遍历 index 纳入 SHA-1（`src/harmony_test_agent/perception/normalizer.py:82-83`），相同语义页面在节点顺序轻微变化后会抖动。建议摘要使用稳定的 `key/id/type/text/bbox/clickable/editable` 数组，先过滤系统节点，再排序并序列化。

规划 key 示例：

```json
{
  "task": "打开知乎++，进入搜索……",
  "profile": {
    "target_app_id": "zhihu-plus",
    "bundle_name": "com.github.zhuoyi233.zhplus",
    "main_ability": "EntryAbility",
    "stable_locator_digest": "sha256:..."
  },
  "max_steps": 20,
  "model": "provider/model",
  "prompt_version": "planning-2026-09-10.1",
  "schema_version": 1
}
```

## 读取、写入与失效规则

采用 cache-aside。Provider 调用前生成 key 并查询；命中时仍使用 Pydantic 模型重新验证 value，验证失败删除条目并按 miss 处理。模型成功且结构化输出通过校验后写入；超时、HTTP 错误、解析错误、`failed_model`、安全拒绝和空结果不写入。写失败只记录指标，不改变 Agent 结果。

缓存不能跳过 SafetyPolicy、bbox 边界检查和元素融合。缓存的 `VisionObservation` 仍经过 `PerceptionService.merge()` 的置信度与屏幕边界判断（`src/harmony_test_agent/perception/service.py:16-53`）；缓存的 plan 仍经过 `align_plan_with_task()` 和最大步数约束；缓存的 decision 仍经过 `_constrain_finish_decision()` 与 `SafetyPolicy.validate_decision()`（`src/harmony_test_agent/agents/orchestrator.py:146-156`、`src/harmony_test_agent/runtime/tools.py:41-46`）。

以下变化必须造成 key 变化或主动失效：模型名、base URL/provider 标识、prompt 文本、Pydantic 输出 schema、Profile stable locators、感知标准化版本、bbox 坐标规范、SafetyPolicy 版本和 `VLM_MIN_CONFIDENCE`。API key 不进入 key，也不写磁盘。

磁盘缓存建议放在 `artifacts/cache/agent-cache.sqlite3`，与运行证据分离。SQLite 使用 WAL、单独表和唯一 key；value 压缩后设置单条大小上限，例如 2 MB，图像本体不重复存储。清理按 expires_at、LRU last_accessed_at 和总字节三层执行。多进程部署前不得使用只靠 Python dict 的缓存，因为各 worker 会产生不一致命中和重复请求。

## 风险分析

陈旧结果是首要风险。VLM 输出不仅由截图决定，还受 hierarchy、模型、prompt、输出 schema 和阈值影响；漏掉任一字段都会把旧 bbox 或旧标签注入新页面。页面中时间、网络内容和个性化信息变化会改变截图哈希，导致低命中，这是正确的保守行为，不应通过模糊图像 hash 强行提高命中率。

动作缓存会放大误点。当前模型决策可在看不到 hierarchy 控件时直接返回坐标（`src/harmony_test_agent/agents/providers.py:36-44`），而执行器会真的点击 bbox 中心或显式坐标（`src/harmony_test_agent/runtime/tools.py:56-63`）。因此跨 Run decision cache 默认禁止；即使同一步重试，也必须绑定 image SHA-256、step、元素摘要和 run ID。

隐私与密钥风险来自用户任务、截图和页面文本。缓存目录继承 artifacts 的本地敏感数据等级，不允许提交 Git；日志不得输出 value；清理命令需要同时支持按年龄和全部清除。若目标应用可能显示账号或个人内容，应支持 `AGENT_CACHE_MODE=off` 和每 Run `cache_bypass=true`。

缓存雪崩和击穿风险可用 per-key single-flight 控制。同一截图并发分析时只有一个请求访问模型，其他等待同一 Future；等待有独立超时，失败后不缓存异常。过期条目不默认 stale-while-revalidate，因为设备动作链需要当前可解释结果；只有纯展示摘要可以另行允许短暂 stale。

当前 Trace 写放大也容易被误称为缓存问题。每个事件都会全量保存 Trace 到 SQLite 和 JSON（`src/harmony_test_agent/runtime/events.py:36-38`），优化方向是事件追加和阶段性 checkpoint，不是增加缓存。两项工作应分开统计和验收。

## 分阶段实施

阶段一只加观测字段和指标，不改变模型调用。给 Provider 包装统一计时器，捕获 Pydantic AI usage；扩展 Run Trace 或独立 metrics artifact，记录 operation、latency、usage、input image bytes 和结果。用现有 Mock 测试验证字段序列化，用 live test 在显式启用时采集真实基线。

阶段二实现 `vision:v1`。增加 `AgentCache` 协议、SQLite 实现和 Null 实现；在 `OpenAICompatibleProvider.analyze()` 外层接入。新增 prompt 常量版本；缓存 value 重新校验后送入现有 merge。测试必须覆盖相同 key 命中、图片变化 miss、hierarchy 变化 miss、模型/prompt/阈值变化 miss、损坏 value 自愈、过期淘汰和并发 single-flight。

阶段三根据基线决定是否增加 `plan:v1`。只有重复任务占比和节省收益显著时上线。decision cache 仍保持 feature flag 关闭；若真实模型供应商会对同一超时请求重复计费，可只做单次 Run 内的 request deduplication。

阶段四增加运维能力：启动时 schema migration，后台有界清理，CLI `cache stats`、`cache prune`、`cache clear --namespace`，报告展示模型调用数、命中率、节省请求数和无法取得 token usage 的明确标记。

## 验收口径

静态验收检查 key 字段完整、缓存不绕过安全校验、异常不落缓存、默认 decision cache 关闭。自动验收使用固定 provider 证明 VLM 第二次调用不触发远端函数，并验证 TTL 和版本失效。真实验收必须在同一设备状态下重复至少 20 轮：报告 hit ratio、模型请求减少比例、P50/P95 延迟变化、缓存字节和错误率，同时确认动作序列及断言结果与 cache off 基线一致。

本次未调用真实模型，无法给出当前 token 成本、命中率或性能收益；仓库也没有这些历史指标。任何百分比收益在完成阶段一前都只能作为目标，不能写成已验证结果。

