# 直流模式（DC Mode）对话交互问题修复方案

> ⚠️ 历史设计文档（2026-09-17 重构前）。当前架构见 docs/ARCHITECTURE.md §16。本文保留仅供追溯。

> 交付对象：执行 Agent。本文档为**可直接实施**的技术方案，包含根因（带文件+行号）、函数级改动、代码模式、边界条件与验证清单。
>
> 分支：`dc-mode`。所有改动限定在 `src/harmony_test_agent/dc/` 与 `web/src/{api,stores,features/dc}/`，**不得触碰 Live Mode 代码路径**（`agents/orchestrator.py`、`runtime/*`、`generation/hypium.py`、`devices/*`、`stores/console.ts`）。

---

## 0. 结论速览（TL;DR）

| 问题 | 根因（一句话） | 核心修复 |
|------|---------------|---------|
| 问题 2：思考/输出不可见 | `provider.py` 用 `agent.run()` 只返回**最终** `result.output`，中间叙述文本与 reasoning 全部丢弃；无 `THINKING` 事件类型 | 改用 `agent.iter()` 逐节点差分 `all_messages()`，实时 emit `THINKING` + `AGENT_TEXT` 事件 |
| 问题 3：截图不更新 | `SCREENSHOT_CAPTURED` 事件 payload **无 `snapshot_path`**；且 `to_view()` 的 `latest_snapshot_path` 是**绝对路径**，前端拼出的 artifact URL 非法 | 事件与视图统一改用**会话相对 POSIX 路径**；前端按事件更新 `latestScreenshotUrl` |
| 问题 4：对话流不透明 | 无流式；前端只处理 `tool_call_finished`（忽略 `tool_call_started`）；增量事件与 `refreshSession` 全量重建**双写冲突** | 工具消息按 `invocation_id` 就地更新（running→done）；单一数据源；ChatGPT 式分块渲染 |
| 问题 1：未参考 artemis | 缺少 artemis 的"思考/输出分离 + 统一事件流 + 步骤块聚合"模式 | 全文以 artemis 模式为蓝本（见 §2） |

**实施顺序**：Phase 1（事件管道修复，必做，风险低）→ Phase 2（`agent.iter()` 实时步骤流，核心）→ Phase 3（前端 ChatGPT 式渲染）。Phase 1 完成后问题 2/3 即可基本可见；Phase 2/3 完成后达到"WebLLM 级透明"。

---

## 1. 现状数据流（修复前）

```
用户输入
  → DcChat.handleSend → store.sendMessage（乐观追加 user 气泡）
  → POST /api/dc/sessions/{id}/messages（router.py:130）
  → asyncio.create_task(_run_turn)（router.py:144-153）
  → DcSession.handle_user_message（session.py:229）
       ├─ emit TURN_STARTED（session.py:251）           ✅ 前端处理（dc-console.ts:238）
       ├─ _capture_context（session.py:353）
       │    └─ emit SCREENSHOT_CAPTURED {width,height,sha256}（session.py:368）  ❌ 无 snapshot_path
       ├─ provider.chat（provider.py:68）
       │    └─ agent.run(...) 一次性跑完整个"模型→工具→模型"循环（provider.py:91）
       │         └─ 工具函数内经 recorder 实时 emit TOOL_CALL_STARTED/FINISHED（tools.py:127,178） ✅ 但前端只处理 FINISHED
       │    └─ return DcChatResponse(output_text=result.output)（provider.py:105）  ❌ 中间叙述/thinking 丢失
       ├─ emit ASSISTANT_MESSAGE {summary}（session.py:311）  ✅ 但仅最终一条
       └─ emit TURN_FINISHED（session.py:316）
  → SSE /events（router.py:185）→ DcEventStream → store.appendEvent（dc-console.ts:222）
```

**三处断点**：① 截图事件无路径；② 模型中间文本/思考从未成为事件；③ 前端工具消息只增不改 + 与 `refreshSession` 全量重建冲突。

---

## 2. artemis 可借鉴模式（提炼自 `D:\Work\Code\Harmony\artemis`）

| artemis 模式 | 文件锚点 | 迁移到 DC 的做法 |
|-------------|---------|-----------------|
| **思考/输出分离**：一次模型响应拆成 `thinking`(原生推理) 与 `raw_thinking/text`(可见输出) 两类独立事件 | `data_engine/trace.py:334-371` | 新增 `THINKING` 与 `AGENT_TEXT` 两种 `DcEventType`，从 `ModelResponse.parts` 的 `part_kind=='thinking'` / `'text'` 分别提取 |
| **反应式循环**：`while` 逐轮"观察→思考→行动→记录"，每步分配 `step_id` | `agents/flash/runner.py:1036-1174` | 用 `agent.iter()` 逐节点遍历，每个 ModelResponse 出现即 emit，天然带时序 |
| **统一事件流 + trace_id 就地更新**：工具 `running→success/failed` 用同一 `trace_id` 覆盖，不新增 | `tools/tool_wrapper.py:106-162`；前端 `agent.service.ts:1067-1077` | 工具消息以 `invocation_id` 为 key，`tool_call_started` 建卡（running），`tool_call_finished` 就地改状态 |
| **步骤块聚合**：每步一块，块内时序渲染 Thought→Work→工具卡→前后截图 | `agent-stream.component.html:654-808` | 前端按 `turn_id` 分组，块内按事件时间序渲染 |
| **截图随轮次刷新**：动作后截图成为下一轮观察 | `agents/flash/runner.py:848-851` | 已有 `_capture_context`，补齐事件路径即可 |
| **流式打字机**：`stream_output` 按 `execution_id\|stream_type` 缓冲，非流事件前先 flush 保序 | `services/llm.py:673-747`；`agent.service.ts:1038-1140` | Phase 3 可选：`node.stream()` + `PartDeltaEvent` |
| **断线重连补全**：SSE replay 已记录步骤 + 前端 backfill | `tasks.py:455-464`；`agent.service.ts:774-779` | 已有 `Last-Event-ID` replay（router.py:205-211），补前端重连后 `refreshSession` 对账 |
| **历史分区压缩**：S(静态)+F(压缩)+A(活动)+T(当前)，旧截图就地替换为文本摘要 | `memory/transcript.py:254-798` | 已有 `_prune_history`（session.py:108），保持；本方案不改 |

---

## 3. 逐问题根因分析（含文件+行号）

### 问题 2：模型思考内容和输出不可见

**根因 2.1 — `provider.py:91-109` 只取最终输出。**
`agent.run()` 内部完成整个多步循环，仅返回 `result.output`（最后一条文本）。模型在每次工具调用**之前/之间**产生的叙述文本（如"我先点击搜索框"）虽存在于 `result.all_messages()`，但从未被提取；推理模型的 `reasoning_content`（`ThinkingPart`）被完全丢弃。

```python
# provider.py:91-109（现状）
result = await agent.run(user_content, message_history=..., deps=..., usage_limits=...)
tool_call_count = _count_tool_calls(result)
return DcChatResponse(output_text=result.output, history=result.all_messages(), tool_call_count=...)
#                      ^^^^^^^^^^^^^^^^^^^^^^^ 只有最终输出，无中间 text/thinking
```

**根因 2.2 — `models.py:174-188` 无思考类事件类型。** `DcEventType` 只有 `ASSISTANT_MESSAGE`，缺 `THINKING` / `AGENT_TEXT`。

**根因 2.3 — `session.py:311-315` 仅在循环结束后 emit 一次 `ASSISTANT_MESSAGE`。** 整个多步执行期间，除工具事件外无任何模型侧事件，用户看不到"模型在想什么/说什么"。

**根因 2.4 — 无流式。** `agent.run()` 阻塞式一次性返回，最终输出也是"憋到最后一次性出现"，而非逐字。

> 注：前端 `dc-console.ts:243-258` 对 `assistant_message` 的处理**本身正确**（取 `payload.summary` 追加 assistant 气泡）。所以"最终输出"理论上可见；但若模型在纯工具调用后不再产出额外文本，`result.output` 可能为空，则**什么都没有**。真正缺失的是**中间思考与叙述**。

### 问题 3：右侧设备截图不更新

**根因 3.1 — `session.py:368-372` 的 `SCREENSHOT_CAPTURED` payload 无 `snapshot_path`。**

```python
# session.py:368-372（现状）——缺 snapshot_path
self._emit(
    DcEventType.SCREENSHOT_CAPTURED,
    "已采集设备截图",
    {"width": width, "height": height, "sha256": self.snapshot_holder.latest_sha256},
)
```

前端 `dc-console.ts:231-236` 恰恰依赖 `event.payload.snapshot_path`：

```ts
// dc-console.ts:231-236
if (event.type === "screenshot_captured") {
  const snapshotPath = event.payload.snapshot_path as string | undefined;  // ← undefined
  if (snapshotPath && state.activeSessionId) {                             // ← 永不进入
    patch.latestScreenshotUrl = dcArtifactUrl(state.activeSessionId, snapshotPath);
  }
}
```

**根因 3.2 — `tools.py:281-293` 的 `tool_screenshot` 事件同样无 `snapshot_path`**（有 `snapshot_id` 但无可下载路径）。对话中模型主动调 `screenshot` 工具时，截图也不刷新。

**根因 3.3 — `session.py:444` 的 `latest_snapshot_path` 是绝对路径。**

```python
# session.py:444（现状）
latest_snapshot_path = str(self.snapshot_holder.latest_path)  # 绝对路径 D:\...\screens\dc_123.jpeg
```

前端 `dc-console.ts:138-139` 用它拼 `dcArtifactUrl(sessionId, 绝对路径)` → URL 含盘符/反斜杠 → 命中 `router.py:274-288` 的 `{artifact_path:path}` 时 URL 编码混乱，`is_relative_to` 校验易失败 → 404。即便 `refreshSession` 被调用也加载不出。

**根因 3.4 — `refreshSession` 在截图事件后从不触发。** `appendEvent` 的 `screenshot_captured` 分支只尝试（失败的）路径更新，不会 `refreshSession`，故 `to_view()` 里的路径也拉不到。

### 问题 4：对话流应像 WebLLM 一样透明

**根因 4.1 — 前端只处理 `tool_call_finished`，忽略 `tool_call_started`。** `dc-console.ts:260-272` 仅在工具**完成后**追加一条消息；后端 `tools.py:127-131` 发的 `TOOL_CALL_STARTED`（含 args）被丢弃 → 无"工具执行中"状态。

**根因 4.2 — 工具消息只增不改，无 running→done 就地更新。** 与 artemis 的 `trace_id` 去重覆盖相反。

**根因 4.3 — 增量事件与 `refreshSession` 全量重建双写冲突。** `sendMessage` 乐观追加 user 气泡（`dc-console.ts:159`），SSE 增量追加 tool/assistant（`dc-console.ts:256,271`），而 `refreshSession` 用 `aggregateMessages` 从 `turns+invocations` **完全重建**（`dc-console.ts:132`）。两路径消息 id 体系不同（`local-*`/`evt-*`/`tool-*` vs `turn_id`/`invocation_id`），切换/刷新时产生**重复或闪烁**。

**根因 4.4 — 无细粒度状态与富工具卡。** `DcChat.tsx:61-68` 只有一个笼统 busy 指示；工具块（`DcChat.tsx:122-146`）只显示一行摘要 + args JSON，无执行结果、无关联截图、无耗时进度。

### 问题 1：未参考 artemis

贯穿性问题——当前实现缺少 artemis 的三大支柱：**思考/输出分离**、**统一事件流 + trace_id 就地更新**、**步骤块聚合渲染**。§2 已给出逐条迁移映射，§4 落地。

---

## 4. 修复方案（分阶段、函数级）

### Phase 1 — 事件管道修复（必做，风险低，解决问题 3 + 问题 2 的"输出可见"）

#### 1.1 `dc/models.py`：新增事件类型

在 `DcEventType`（`models.py:174-188`）追加：

```python
class DcEventType(StrEnum):
    # ... 既有类型保持 ...
    THINKING = "thinking"  # 模型原生推理（reasoning_content / ThinkingPart）
    AGENT_TEXT = "agent_text"  # 模型可见叙述文本（每步 TextPart，非最终总结）
```

`DC_EVENT_TYPES`（`models.py:191`）自动包含（它由枚举推导）。

#### 1.2 `dc/session.py`：截图事件补路径 + 相对路径助手

**(a) 新增相对路径助手**（放在 `DcSession` 内）：

```python
def _rel_artifact(self, abs_path: Path | None) -> str | None:
    """把绝对产物路径转为会话目录相对 POSIX 路径（供前端 artifact URL 使用）。"""
    if abs_path is None:
        return None
    try:
        return abs_path.resolve().relative_to(self.dir.resolve()).as_posix()
    except ValueError:
        return None
```

**(b) 修复 `_capture_context` 的 SCREENSHOT_CAPTURED**（`session.py:368-372`）：

```python
self.snapshot_holder.update_jpeg(jpeg_path, jpeg_bytes, width, height)
self._emit(
    DcEventType.SCREENSHOT_CAPTURED,
    "已采集设备截图",
    {
        "snapshot_path": self._rel_artifact(jpeg_path),  # ← 新增：会话相对 POSIX 路径
        "width": width,
        "height": height,
        "sha256": self.snapshot_holder.latest_sha256,
        "source": "context",
    },
)
```

**(c) 修复 `to_view()` 的 latest_snapshot_path**（`session.py:444`）：

```python
latest_snapshot_path = self._rel_artifact(self.snapshot_holder.latest_path)  # ← 相对路径
```

**(d) `_capture_context` 截图失败时 emit 明确信号**（`session.py:373-374` 的 except 分支）：

```python
except Exception as exc:
    jpeg_bytes = self.snapshot_holder.latest_jpeg or b""
    self._emit(DcEventType.SCREENSHOT_CAPTURED, "截图采集失败",
               {"snapshot_path": None, "error": f"{type(exc).__name__}: {exc}", "source": "context"})
```

#### 1.3 `dc/tools.py`：tool_screenshot 事件补路径 + 工具结果入 payload

**(a) 修复 `tool_screenshot` 的 SCREENSHOT_CAPTURED**（`tools.py:281-293`）——加入 `snapshot_path`。JPEG 路径由 `hdc.screenshot_jpeg` 返回的 `jpeg_path` 提供，需转相对路径。给 `DcToolContext` 增加 `session_dir: Path` 字段（见 1.4），然后：

```python
rel = jpeg_path.resolve().relative_to(deps.session_dir.resolve()).as_posix()
deps.recorder._emit_event(
    DcEventType.SCREENSHOT_CAPTURED,
    "已采集设备截图",
    {
        "snapshot_path": rel,
        "snapshot_id": snapshot.snapshot_id,
        "width": width,
        "height": height,
        "sha256": deps.snapshot_holder.latest_sha256,
        "changed": changed,
        "element_count": len(snapshot.elements),
        "page_path": snapshot.page_path,
        "source": "tool",
    },
)
```

**(b) `DcActionRecorder.run` 的 TOOL_CALL_FINISHED 补 `args` 与 `result_summary`**（`tools.py:178-188`），让前端就地渲染工具结果：

```python
self._emit_event(
    DcEventType.TOOL_CALL_FINISHED,
    f"工具 {tool.value} {'成功' if success else '失败'}",
    {
        "invocation_id": invocation_id,
        "tool": tool.value,
        "args": args,  # ← 新增
        "success": success,
        "duration_ms": duration_ms,
        "result_summary": result_text[:500],  # ← 新增（截断，避免事件过大）
        "error": error,
    },
)
```

#### 1.4 `dc/tools.py` + `dc/session.py`：DcToolContext 增 session_dir

`DcToolContext`（`tools.py` 的 dataclass）增加字段 `session_dir: Path`；`session.py:265-277` 构造处补 `session_dir=self.dir`。用于 1.3(a) 的相对路径计算。

#### 1.5 前端 `web/src/stores/dc-console.ts`：截图事件更新 URL

修复 `appendEvent` 的 `screenshot_captured` 分支（`dc-console.ts:231-236`）——路径已是相对路径，直接用，并加缓存穿透参数：

```ts
if (event.type === "screenshot_captured") {
  const snapshotPath = event.payload.snapshot_path as string | null | undefined;
  if (snapshotPath && state.activeSessionId) {
    // 文件名唯一（时间戳+uuid），无需额外 cache-bust；RetryImage 亦有 revision 兜底
    patch.latestScreenshotUrl = dcArtifactUrl(state.activeSessionId, snapshotPath);
  }
  // 截图失败（snapshot_path 为空且带 error）时保持上一帧，不清空
}
```

同时修复 `refreshSession` 的路径使用（`dc-console.ts:138-139`）——后端已返回相对路径，无需改动逻辑，但确认 `session.latest_snapshot_path` 现在是相对路径即可。

**Phase 1 验收**：对话中每次采集截图，右侧 `DcScreen` 立即刷新；最终 `output_text` 作为 assistant 气泡显示。（思考/中间叙述仍缺，进入 Phase 2。）

---

### Phase 2 — `agent.iter()` 实时步骤流（核心，解决问题 2 + 问题 4 的时序透明）

> **已验证的 pydantic-ai 1.73 API 事实**（spike 确认，实施时据此编写）：
> - `agent.iter(user_prompt, *, message_history, deps, model_settings, usage_limits)` 返回异步上下文管理器；`async for node in run` 依次产出 `UserPromptNode → ModelRequestNode → CallToolsNode → End`。
> - **被动遍历会自动推进节点**（无需手动 `node.next()`；对 `UserPromptNode` 调 `.next()` 会报 `AttributeError`）。
> - 判定：`agent.is_model_request_node(node)` / `agent.is_call_tools_node(node)`。
> - 在**当前**节点上 `node.model_response` 尚未填充；模型响应在推进到下一节点后可经 `run.all_messages()` 读到。故用**消息差分**：记录已处理消息数 `seen`，每轮取 `run.all_messages()[seen:]` 中的新 `ModelResponse`，遍历其 `parts`。
> - `ModelResponse.parts` 每个 part 是 TypedDict，含 `part_kind`：`'text'`(TextPart, 有 `content`)、`'thinking'`(ThinkingPart, 有 `content`)、`'tool-call'`、`'tool-return'`。
> - `run.result.output` 为最终输出；`run.all_messages()` 为完整历史（回填 `session.history`）。

#### 2.1 `dc/models.py`：DcChatRequest 增 emit 回调

`DcChatRequest`（`models.py`）新增可选字段，让 provider 在执行中实时回传事件：

```python
class DcChatRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    user_prompt: str
    screenshot_jpeg: bytes = b""
    ui_tree_digest: str = ""
    history: list[Any] = Field(default_factory=list)
    tools: list[Any] = Field(default_factory=list)
    tool_context: Any = None
    emit: Any = None  # ← 新增：Callable[[str, str, dict], None] | None，(event_type_value, message, payload)
```

> `DcChatResponse` 保持不变（`output_text` / `history` / `tool_call_count`）。

#### 2.2 `dc/provider.py`：`chat()` 改用 `agent.iter()` 逐节点 emit

重写 `DcChatProvider.chat`（`provider.py:68-109`）。核心逻辑：

```python
async def chat(self, request: DcChatRequest) -> DcChatResponse:
    from .models import DcEventType

    agent: Agent[Any, str] = Agent(
        self._model(vision=True),
        system_prompt=DC_SYSTEM_PROMPT,
        tools=request.tools,
        retries=1,
    )
    user_prompt = DC_TURN_TEMPLATE.format(
        ui_tree=request.ui_tree_digest or "(no UI tree available)",
        prompt=request.user_prompt,
    )
    user_content: list[Any] = [user_prompt]
    if request.screenshot_jpeg:
        user_content.append(BinaryContent(data=request.screenshot_jpeg, media_type="image/jpeg"))

    emit = request.emit  # 可能为 None（如单测直接调用）
    seen = 0
    step = 0

    async with agent.iter(
        user_content,
        message_history=request.history or None,
        model_settings=self._model_settings(),
        deps=request.tool_context,
        usage_limits=UsageLimits(request_limit=30, tool_calls_limit=50),
    ) as run:
        async for node in run:
            # 差分提取本轮新产生的 ModelResponse 的 text/thinking part
            messages = run.all_messages()
            for msg in messages[seen:]:
                for part in getattr(msg, "parts", []) or []:
                    kind = part.get("part_kind") if isinstance(part, dict) else getattr(part, "part_kind", None)
                    content = (part.get("content") if isinstance(part, dict) else getattr(part, "content", "")) or ""
                    if kind == "thinking" and content.strip() and emit:
                        step += 1
                        emit(DcEventType.THINKING.value, content[:200], {"step": step, "text": content})
                    elif kind == "text" and content.strip() and emit:
                        step += 1
                        emit(DcEventType.AGENT_TEXT.value, content[:200], {"step": step, "text": content})
            seen = len(messages)
            # 被动遍历自动推进节点；工具调用由 recorder 实时 emit TOOL_CALL_*

    output_text = run.result.output if run.result else ""
    return DcChatResponse(
        output_text=output_text,
        history=run.all_messages(),
        tool_call_count=_count_tool_calls_from_messages(run.all_messages()),
    )
```

配套：把 `_count_tool_calls`（`provider.py:158-171`）重构为接受 messages 列表的 `_count_tool_calls_from_messages(messages)`，供上面复用（原 `result` 版可保留或改为调用它）。

> **关键纪律（源自 artemis `runner.py:410-412`）**：`THINKING` 仅用于**展示**，绝不回喂模型——pydantic-ai 的历史管理已保证 `ThinkingPart` 正确进出，我们只读不改。

#### 2.3 `dc/provider.py`：MockDcChatProvider 也 emit（供无模型时 UI 联调）

`MockDcChatProvider.chat`（`provider.py:124-134`）增加：

```python
async def chat(self, request: DcChatRequest) -> DcChatResponse:
    from .models import DcEventType

    emit = request.emit
    if emit:
        emit(
            DcEventType.THINKING.value,
            "（Mock）分析用户意图与当前屏幕…",
            {"step": 1, "text": "（Mock 模式）未连接真实模型，跳过实际推理与工具调用。"},
        )
        emit(
            DcEventType.AGENT_TEXT.value,
            "（Mock）已收到消息",
            {"step": 1, "text": "当前为 Mock 模式，配置 OPENAI_API_KEY 与 AGENT_MODEL 后启用真实执行。"},
        )
    text = (
        "任务完成（Mock）：已收到用户消息。当前为 Mock 模式，未实际调用工具或操作设备。"
        "请配置 OPENAI_API_KEY 和 AGENT_MODEL 以启用真实模型。"
    )
    return DcChatResponse(output_text=text, history=list(request.history or []), tool_call_count=0)
```

#### 2.4 `dc/session.py`：把 emit 回调注入 DcChatRequest

`handle_user_message` 构造 request 处（`session.py:283-290`）增加 `emit`：

```python
request = DcChatRequest(
    user_prompt=text,
    screenshot_jpeg=jpeg_bytes,
    ui_tree_digest=ui_tree_digest,
    history=pruned_history,
    tools=tools,
    tool_context=tool_context,
    emit=lambda etype, msg, payload: self._emit(DcEventType(etype), msg, {**payload, "turn_id": turn_id}),
)
```

> `self._emit` 签名是 `(DcEventType, str, dict)`；这里把字符串事件名转回枚举，并统一附加 `turn_id` 便于前端按轮分组。

**Phase 2 验收**：对话中依次实时出现——用户气泡 →（思考块）→ 模型叙述 → 工具卡（running→done）→ …循环… → 最终回复。全部按时间序，非"憋到最后"。

---

### Phase 3 — 前端 ChatGPT 式透明渲染（解决问题 4 的呈现）

#### 3.1 `web/src/api/dc-types.ts`：扩展类型

```ts
// DcEventType 联合追加：
export type DcEventType = /* 既有 */ | "thinking" | "agent_text";

// DC_EVENT_TYPES 数组追加 "thinking", "agent_text"（SSE 订阅用）

// DcChatMessage 扩展：
export type DcMessageRole = "user" | "assistant" | "thinking" | "narration" | "tool";
export interface DcChatMessage {
  id: string;                 // 工具消息用 invocation_id 作 key（就地更新）
  role: DcMessageRole;
  content: string;
  timestamp: string;
  turnId?: string;
  step?: number;
  // 工具专属
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolResult?: string;
  toolStatus?: "running" | "success" | "failed";
  durationMs?: number;
  screenshotUrl?: string;     // 关联截图缩略图
}
```

#### 3.2 `web/src/stores/dc-console.ts`：单一数据源 + 就地更新

**核心原则**：**进行中轮次**由增量事件驱动；**历史轮次**由 `refreshSession`/`aggregateMessages` 驱动。二者以 `turn_id` 边界隔离，避免双写冲突（根因 4.3）。

改造 `appendEvent`（`dc-console.ts:222-286`）：

```ts
appendEvent: (event) => set((state) => {
  if (state.events.some((e) => e.event_id === event.event_id)) return state;
  const events = [...state.events, event];
  const patch: Partial<DcState> = { events };
  const p = event.payload as Record<string, any>;

  switch (event.type) {
    case "screenshot_captured": {
      const rel = p.snapshot_path as string | null;
      if (rel && state.activeSessionId) {
        patch.latestScreenshotUrl = dcArtifactUrl(state.activeSessionId, rel);
      }
      break;
    }
    case "turn_started":
      patch.status = "thinking"; patch.sending = true;
      // 乐观 user 气泡已在 sendMessage 追加；此处不再重复
      break;
    case "thinking":
      patch.messages = [...state.messages, {
        id: `think-${event.event_id}`, role: "thinking",
        content: (p.text as string) ?? event.message, timestamp: event.timestamp,
        turnId: p.turn_id, step: p.step,
      }];
      break;
    case "agent_text":
      patch.messages = [...state.messages, {
        id: `text-${event.event_id}`, role: "narration",
        content: (p.text as string) ?? event.message, timestamp: event.timestamp,
        turnId: p.turn_id, step: p.step,
      }];
      break;
    case "tool_call_started": {
      // 建卡（running）——以 invocation_id 为 key
      const inv = p.invocation_id as string;
      patch.messages = [...state.messages, {
        id: inv, role: "tool", content: p.tool as string, timestamp: event.timestamp,
        toolName: p.tool, toolArgs: p.args, toolStatus: "running",
      }];
      patch.status = "acting";
      break;
    }
    case "tool_call_finished": {
      // 就地更新同 invocation_id 的卡（running→success/failed），不新增（根因 4.2）
      const inv = p.invocation_id as string;
      patch.messages = state.messages.map((m) =>
        m.id === inv
          ? { ...m, toolStatus: p.success ? "success" : "failed",
              toolResult: p.result_summary, durationMs: p.duration_ms,
              content: `${p.tool} ${p.success ? "✓" : "✗"} (${p.duration_ms}ms)` }
          : m,
      );
      // 若找不到（started 丢失），兜底追加
      if (!state.messages.some((m) => m.id === inv)) {
        patch.messages = [...state.messages, {
          id: inv, role: "tool", content: `${p.tool}`, timestamp: event.timestamp,
          toolName: p.tool, toolArgs: p.args, toolStatus: p.success ? "success" : "failed",
          toolResult: p.result_summary, durationMs: p.duration_ms,
        }];
      }
      break;
    }
    case "assistant_message":
      patch.messages = [...state.messages, {
        id: `final-${event.event_id}`, role: "assistant",
        content: (p.summary as string) ?? event.message, timestamp: event.timestamp, turnId: p.turn_id,
      }];
      break;
    case "turn_finished":
      patch.status = "idle"; patch.sending = false;
      break;
    case "error":
      patch.error = event.message; patch.status = "idle"; patch.sending = false;
      break;
    case "script_generated":
      void get().refreshSession(true);
      break;
  }
  return { ...state, ...patch };
}),
```

**去重对账**：`refreshSession`（`dc-console.ts:126-146`）重建历史时，`aggregateMessages`（`dc-console.ts:310-345`）需产出与增量事件**一致的 id 体系**——工具消息 id 用 `inv.invocation_id`（已如此，`dc-console.ts:325`），助手最终消息 id 用 `${turn.turn_id}-summary`。为避免"进行中轮次被 refresh 覆盖"，`refreshSession` 应**只重建已完成轮次**：若 `session.status` 为 `thinking/acting`，保留当前增量 messages，仅更新 `session`/`script`/`latestScreenshotUrl`，不覆盖 `messages`。

```ts
refreshSession: async (quiet = false) => {
  const { activeSessionId } = get();
  if (!activeSessionId) return;
  try {
    const session = await getDcSession(activeSessionId);
    const inFlight = session.status === "thinking" || session.status === "acting";
    const patch: Partial<DcState> = {
      session, status: session.status, script: session.script ?? null,
      latestScreenshotUrl: session.latest_snapshot_path
        ? dcArtifactUrl(activeSessionId, session.latest_snapshot_path) : get().latestScreenshotUrl,
      ...(quiet ? {} : { error: "" }),
    };
    if (!inFlight) patch.messages = aggregateMessages(session);  // 仅空闲时用全量重建对账
    set(patch);
  } catch (cause) {
    if (!quiet) set({ error: apiError("刷新 DC 会话失败", cause) });
  }
},
```

同时 `aggregateMessages` 增加对 thinking/narration 的还原：由于 `DcTurnRecord` 目前不含分步 text/thinking，**Phase 3 可选增强**——在 `DcTurnRecord` 增加 `steps: list[DcStepRecord]`（后端 `session.py` 记录每步 text/thinking），使刷新后历史也保留思考块。若不做，历史轮次刷新后思考块会丢失（仅进行中可见）——**在文档验证清单中标注此限制**。

#### 3.3 `web/src/features/dc/DcChat.tsx`：分角色渲染

改造 `MessageBubble`（`DcChat.tsx:107-158`）支持 5 种角色：

- `user`：右对齐气泡（现状保留）。
- `thinking`：**可折叠**灰色块，标题"💭 思考"，默认展开，`turn_finished` 后自动折叠（用 `useState` + 监听 status）。参照 artemis Thought 块（`agent-stream.component.html:700-754`）。
- `narration`：左对齐浅色气泡，Markdown 渲染模型叙述。
- `tool`：**工具卡**——`toolStatus==='running'` 显示旋转 spinner + "调用中…"；`success/failed` 显示 ✓/✗ + 工具名 + 参数（折叠）+ `toolResult`（折叠）+ 耗时。参照 artemis 工具卡（`tool_wrapper.py:106-162` 的 running→success 语义）。
- `assistant`：左对齐气泡，Markdown 渲染最终回复（现状保留）。

busy 指示器（`DcChat.tsx:61-68`）文案按 `status` 细化：`thinking`→"Agent 正在思考…"，`acting`→"Agent 正在执行工具…"。

#### 3.4 `web/src/features/dc/DcScreen.tsx`：截图刷新

`DcScreen.tsx` 逻辑不变（已读 `latestScreenshotUrl`）。为确保 `RetryImage` 在 URL 变化时重载：`RetryImage`（`components/ui/primitives.tsx:46-71`）已用 `useEffect([src])` 重置 `revision/failed`，故 URL 变化即重载，无需改动。若同一 URL 需强制刷新，`dcArtifactUrl` 结果追加 `?t=${Date.now()}`（但正常每帧文件名唯一，不需要）。

#### 3.5（可选）流式打字机

若要 artemis 级逐字输出：`provider.chat` 的 ModelRequestNode 改用 `async with node.stream(run.ctx) as stream`，监听 `PartStartEvent/PartDeltaEvent(TextPartDelta.content_delta)/PartEndEvent`，emit `AGENT_TEXT_DELTA` 事件（新增类型）；前端按 `step` 缓冲追加、遇非流事件先 flush（参照 `agent.service.ts:810-814`）。**本方案默认不启用**（Phase 2 的分步全文 emit 已满足"时序透明"，流式为增量优化）。

---

## 5. 边界条件处理

| 场景 | 处理策略 | 落点 |
|------|---------|------|
| **模型未配置（Mock）** | `MockDcChatProvider.chat` emit 一条 THINKING + AGENT_TEXT + 最终 ASSISTANT_MESSAGE，UI 正常显示"Mock 模式"提示，不报错 | provider.py 2.3 |
| **设备断开** | `_capture_context` 截图异常 → emit `SCREENSHOT_CAPTURED{snapshot_path:null,error}`；前端保留上一帧 + 顶部显示"设备画面不可用"；`hdc.screenshot_jpeg` 3 次重试后抛 `DeviceError`，被 `handle_user_message` 的 except 捕获 → emit ERROR | session.py 1.2(d)；DcScreen |
| **截图从未成功** | `latestScreenshotUrl` 保持 `null`，`DcScreen` 显示 EmptyState（现状 `DcScreen.tsx:20-22`） | 无需改 |
| **长对话性能** | ① 消息 > 200 条时 `DcChat` 只渲染最近 200 条 + "加载更早"按钮；② `events` 数组仅用于去重，可定期截断（保留最近 500）；③ 后端 `_prune_history`（session.py:108）已控制模型侧 token | DcChat / dc-console |
| **SSE 断线重连** | `DcEventStream`（`api/dc-sse.ts`）已限次重连；浏览器 EventSource 自动带 `Last-Event-ID` → 后端 `router.py:205-211` replay recent buffer。**补充**：重连成功（首帧到达）后触发一次 `refreshSession(true)` 对账，防止 buffer 溢出（>500 事件）丢失的历史 | dc-sse.ts / dc-console |
| **并发消息发送** | 后端双保险：`router.py:139-140` status∈(thinking,acting) 返回 409；`session.py:240` `async with self.lock` 串行化。前端：`DcChat.tsx:79` busy 时禁用输入 + `sendMessage` 检查 `sending` | 现状已具备，验证即可 |
| **工具超时/失败** | `recorder.run`（tools.py:151-158）捕获 `TimeoutError`/`Exception` → `success=False`+`error`；TOOL_CALL_FINISHED 带 error（1.3b）；前端工具卡红色 ✗ + 展开显示 error | tools.py / DcChat |
| **emit 回调为 None** | `provider.chat` 内所有 emit 前判 `if emit:`（2.2/2.3），保证单测直接调用 `chat()` 不崩 | provider.py |
| **reasoning 模型无 thinking** | 无 `ThinkingPart` 时不 emit THINKING，仅有 AGENT_TEXT/最终输出，UI 不显示思考块（正常降级） | provider.py 2.2 |
| **事件乱序/丢失** | 前端按 `event_id` 去重（dc-console.ts:225）；工具卡以 `invocation_id` 幂等更新（started 丢失时 finished 兜底建卡，3.2） | dc-console |

---

## 6. 验证清单

### 6.1 自动化测试（新增/扩展，`tests/`）

| 测试 | 步骤 | 预期 |
|------|------|------|
| `test_dc_provider_iter_emits`（新，unit） | 用 `TestModel`/`FunctionModel` 构造带 emit 回调的 `DcChatRequest`，调 `DcChatProvider.chat`（或 mock 版） | emit 至少被调用；THINKING/AGENT_TEXT/ASSISTANT_MESSAGE 事件按序产生 |
| `test_dc_screenshot_event_has_path`（新，unit） | mock `hdc.screenshot_jpeg` 返回已知路径，调 `_capture_context`，捕获 bus 事件 | `SCREENSHOT_CAPTURED.payload.snapshot_path` 为**相对 POSIX 路径**（如 `screens/dc_x.jpeg`），非绝对、无反斜杠 |
| `test_dc_to_view_relative_path`（新，unit） | 设置 `snapshot_holder.latest_path` 为绝对路径，调 `to_view()` | `latest_snapshot_path` 为会话相对 POSIX 路径 |
| `test_dc_tool_finished_payload`（新，unit） | 触发一次工具调用，检查 TOOL_CALL_FINISHED 事件 | payload 含 `args` 与 `result_summary` |
| `test_dc_contract.py`（扩展，api） | 复用现有 fixture，POST 消息后消费 SSE | 事件序列含 `turn_started`→（`screenshot_captured` 带 snapshot_path）→`assistant_message`→`turn_finished` |
| 既有回归 | `pytest tests -x --ignore=tests/live` | 297+ 全绿；`test_dc_isolation.py` 仍通过 |

### 6.2 前端测试（`web/src/features/dc/`）

| 测试 | 步骤 | 预期 |
|------|------|------|
| `dc-store.test.ts`（新） | 依次 dispatch `screenshot_captured`(带相对路径)/`thinking`/`agent_text`/`tool_call_started`/`tool_call_finished`/`assistant_message` | `latestScreenshotUrl` 更新；messages 含 thinking/narration/tool/assistant；工具卡由 running 就地变 success（**同 id，不新增**） |
| `dc-store.test.ts` 去重 | 同一 `event_id` dispatch 两次 | messages 不重复 |
| `DcChat.test.tsx`（新） | 注入含 5 种角色的 messages 渲染 | 思考块可折叠；工具卡显示 ✓/✗ + 耗时；assistant 用 Markdown |

### 6.3 手工/浏览器验收（真实或 Mock）

1. **问题 3 验证**：`?tab=dc` → 新建会话 → 发送"截个图看看当前界面" → 右侧 `DcScreen` **1 秒内**显示设备截图；再发一条触发新截图 → 右侧**刷新为新帧**（非停留在旧图/空态）。DevTools Network 中 artifact 请求返回 **200**（非 404）。
2. **问题 2 验证（Mock）**：无模型配置下发送任意消息 → 对话流出现"💭 思考（Mock）"块 + 叙述 + 最终"任务完成（Mock）"气泡。
3. **问题 2 验证（真实模型）**：配置 `OPENAI_API_KEY`+`AGENT_MODEL`（支持图像）→ 发送"打开设置并返回首页" → 对话流**实时**依次出现：用户气泡 → 思考/叙述 → 工具卡（start_app running→✓）→ 截图刷新 → …→ 最终总结气泡。
4. **问题 4 验证**：整个多步执行过程中，每一步**按时间序逐步出现**（非全部憋到最后一次性弹出）；工具卡在调用中显示 spinner，完成后变 ✓ 并附结果与耗时。
5. **并发保护**：执行中再次点发送 → 输入框禁用；直接 POST `/messages` → **409**。
6. **断线重连**：执行中断网 2 秒再恢复 → SSE 自动重连，`Last-Event-ID` replay 补齐缺失事件，消息不重复不丢失。
7. **设备断开**：关闭模拟器后发消息 → 顶部/截图区显示设备不可用提示，对话流出现 ERROR，不白屏、不卡死。
8. **隔离回归**：DC 全流程操作后，切到"实时执行"Tab，Live Mode 的运行/历史/Profile 功能**完全正常**，`/api/runs` 未被污染。

### 6.4 质量门禁（合并前，遵循 AGENTS.md）

```powershell
uv lock --check
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m pytest -q --ignore=tests/live
Push-Location web; npm run build; npm run test; Pop-Location
git diff --check
```

---

## 7. 改动文件清单（汇总）

**后端（修改）**：
- `src/harmony_test_agent/dc/models.py`：`DcEventType` +THINKING/+AGENT_TEXT；`DcChatRequest` +emit 字段
- `src/harmony_test_agent/dc/provider.py`：`chat()` 改 `agent.iter()` 逐节点 emit；Mock 版 emit；`_count_tool_calls` 重构
- `src/harmony_test_agent/dc/session.py`：`_rel_artifact` 助手；`_capture_context` 截图事件补 snapshot_path（含失败分支）；`to_view` 相对路径；`handle_user_message` 注入 emit；`DcToolContext` 构造补 `session_dir`
- `src/harmony_test_agent/dc/tools.py`：`DcToolContext` +session_dir 字段；`tool_screenshot` 事件补 snapshot_path；`TOOL_CALL_FINISHED` payload +args/+result_summary

**前端（修改）**：
- `web/src/api/dc-types.ts`：+thinking/agent_text 事件类型；`DcChatMessage` 扩展 role 与工具字段
- `web/src/stores/dc-console.ts`：`appendEvent` 全量改造（就地更新工具卡、thinking/narration、截图路径）；`refreshSession` 进行中轮次不覆盖 messages；`aggregateMessages` id 体系对齐
- `web/src/features/dc/DcChat.tsx`：`MessageBubble` 支持 5 角色；思考块折叠；工具卡 running→done；busy 文案细化
- `web/src/features/dc/DcScreen.tsx`：无需改（依赖 store 修复）
- `web/src/api/dc-sse.ts`：重连成功后触发 `refreshSession(true)` 对账（可选）

**测试（新增）**：`tests/unit/test_dc_provider_iter.py`、`tests/unit/test_dc_screenshot_path.py`、扩展 `tests/api/test_dc_contract.py`、`web/src/features/dc/dc-store.test.ts`、`web/src/features/dc/DcChat.test.tsx`

**不改动**（隔离红线）：`agents/orchestrator.py`、`runtime/*`、`generation/hypium.py`、`devices/*`、`models.py`(顶层)、`stores/console.ts`、`api/sse.ts`、`api/types.ts`、所有 `features/{live,advisor,graph,...}`。

---

## 8. 实施顺序与依赖

```
Phase 1（必做，独立可验收）:
  1.1 models.py 事件类型 ─┐
  1.2 session.py 截图路径 ─┼─→ 1.5 dc-console 截图 URL ─→ 【问题3 解决】
  1.3 tools.py 事件补全 ──┤
  1.4 DcToolContext session_dir ┘

Phase 2（依赖 Phase 1 的事件类型）:
  2.1 DcChatRequest.emit ─→ 2.2 provider.iter ─→ 2.4 session 注入 emit ─→ 【问题2 思考/输出实时可见】
                          └→ 2.3 Mock emit

Phase 3（依赖 Phase 2 的事件流）:
  3.1 dc-types 扩展 ─→ 3.2 dc-console 就地更新/单一数据源 ─→ 3.3 DcChat 分角色渲染 ─→ 【问题4 透明对话流】
                                              └→ 3.4 DcScreen（免改）
  3.5 流式打字机（可选，最后）
```

**关键路径**：1.1 → 1.2/1.3 → 2.1 → 2.2 → 2.4 → 3.2 → 3.3。

每个 Phase 结束跑一次 §6.4 门禁 + 对应 §6 验收，绿了再进下一 Phase。
