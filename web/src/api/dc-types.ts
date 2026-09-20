// DC 模式专属 TypeScript 类型。
// 不追加到 api/types.ts（隔离要求）；DC_EVENT_TYPES 为本地常量。

/** 工具层级 L1-L5 */
export type DcToolTier = 1 | 2 | 3 | 4 | 5;

/** 工具名称（23 个） */
export type DcToolName =
  | "click" | "swipe" | "input_text" | "key_event" | "back" | "wait" | "screenshot" | "inspect_screen"
  | "dump_ui_hierarchy" | "collect_logs" | "foreground_app" | "list_apps" | "inspect_app" | "memory_dump"
  | "start_app" | "force_stop_app" | "install_app" | "uninstall_app" | "clear_app_data"
  | "file_send" | "file_recv" | "file_list"
  | "execute_shell";

/**
 * 工具调用状态机（计划第 5.1 节）。
 * `running` 表示尚未结束；旧服务可能只给 `success: boolean`，需要归一化。
 */
export type DcToolStatus = "running" | "succeeded" | "failed" | "timed_out" | "cancelled" | "unknown";

/** 设备副作用确认状态：未知副作用不允许自动重放 */
export type DcEffectStatus = "none" | "confirmed" | "unknown";

/**
 * 工具调用录制记录。
 * 新增字段全部可选，兼容只返回 `success` 的旧服务/旧快照；
 * running 记录 `ended_at` 为 null，耗时由前端按 `started_at` 推导。
 */
export interface DcToolInvocation {
  invocation_id: string;
  turn_id: string;
  tool: DcToolName;
  tier: DcToolTier;
  args: Record<string, unknown>;
  /* 兼容字段：旧服务只有布尔；新服务以 status 为权威 */
  success: boolean;
  started_at: string;
  /** running 时为 null */
  ended_at: string | null;
  duration_ms: number;
  error?: string | null;
  before_snapshot_id?: string | null;
  after_snapshot_id?: string | null;
  /* ---- 新增：实时状态与进度 ---- */
  status?: DcToolStatus;
  last_progress_at?: string | null;
  deadline_at?: string | null;
  effect_status?: DcEffectStatus;
  command_id?: string | null;
  /** 当前阶段，如 snapshot_display / file_recv / waiting_model */
  phase?: string;
  result_summary?: string;
  cancellable?: boolean;
  error_code?: string | null;
  /** 底层命令结果（旧字段，展示时可选） */
  command?: unknown;
}

/** 对话轮次状态 */
export type DcTurnStatus =
  | "running" | "completed" | "blocked" | "needs_user" | "failed" | "cancelled"
  /** 执行环境中断（进程重启、SSE 断开、会话关闭） */
  | "interrupted"
  /** 设备副作用未知，必须人工确认后才能继续 */
  | "needs_attention";

/**
 * 跨轮次的公开连续性摘要（计划第 6 节 Phase 3）。
 * 只包含用户可见事实；隐藏推理原文绝不进入本摘要。
 */
export interface DcContinuationContext {
  previous_turn_id: string;
  previous_status: DcTurnStatus;
  original_user_goal: string;
  public_agent_summary: string;
  public_agent_steps: string[];
  completed_operations: string[];
  active_or_unknown_operation: string | null;
  last_snapshot_path: string | null;
  last_page_path: string | null;
  last_foreground_app: string | null;
  last_progress_at: string | null;
  effect_status: DcEffectStatus;
  reconcile_required: boolean;
  context_version: number;
  updated_at: string;
}

/** 模型侧步骤类型：原生推理 / 可见叙述 */
export type DcStepKind = "thinking" | "agent_text";

/** 模型侧单步记录（刷新历史时用于还原思考/叙述块） */
export interface DcStepRecord {
  step: number;
  kind: DcStepKind;
  text: string;
}

/** 对话轮次记录 */
export interface DcTurnRecord {
  turn_id: string;
  user_message: string;
  status: DcTurnStatus;
  agent_summary: string;
  started_at: string;
  ended_at?: string | null;
  invocation_ids: string[];
  steps?: DcStepRecord[];
  error?: string | null;
}

/** 事件类型 */
export type DcEventType =
  | "session_created" | "session_closed"
  | "turn_started" | "turn_finished"
  | "turn_cancel_requested" | "turn_interrupted"
  | "context_capture_started" | "context_capture_finished" | "context_checkpoint"
  | "model_call_started" | "model_call_progress" | "model_call_finished" | "model_call_failed"
  | "tool_call_started" | "tool_call_progress" | "tool_call_finished"
  | "screenshot_captured" | "ui_tree_captured"
  | "assistant_message" | "thinking" | "agent_text" | "message_delta"
  | "token_usage_updated"
  | "script_generated" | "tier_changed" | "needs_attention" | "error";

/**
 * `message_delta` 事件 payload 契约（token 级流式增量）：
 * 同一模型消息的思考与文本各有一个 `stream_key`（`{turn_id}:m{index}:thinking|:text`）。
 * 文本 key 的 `role` 可能随 tool-call part 的出现由 `assistant` 改判为 `narration`。
 *
 * 既有 `thinking` / `agent_text` 全量事件的 payload 追加 `stream_key` 字段（与对应草稿一致），
 * 前端据此把草稿就地收口为全量文本；`assistant_message` 仍是终态权威文本。
 */
export interface DcMessageDeltaPayload {
  turn_id: string;
  stream_key: string;
  role: "thinking" | "narration" | "assistant";
  delta: string;
  message_index: number;
}

/** DC 事件类型列表（用于 SSE 订阅；新增事件类型必须同步加入，否则收不到） */
export const DC_EVENT_TYPES: DcEventType[] = [
  "session_created", "session_closed",
  "turn_started", "turn_finished",
  "turn_cancel_requested", "turn_interrupted",
  "context_capture_started", "context_capture_finished", "context_checkpoint",
  "model_call_started", "model_call_progress", "model_call_finished", "model_call_failed",
  "tool_call_started", "tool_call_progress", "tool_call_finished",
  "screenshot_captured", "ui_tree_captured",
  "assistant_message", "thinking", "agent_text", "message_delta",
  "token_usage_updated",
  "script_generated", "tier_changed", "needs_attention", "error",
];

/** DC 事件 */
export interface DcEvent {
  event_id: number;
  session_id: string;
  type: DcEventType;
  timestamp: string;
  message: string;
  payload: Record<string, unknown>;
}

/** 事件信封可选的统一字段（schema_version >= 2 的服务端会带上） */
export interface DcEventEnvelopeExtras {
  schema_version?: number;
  sequence?: number;
  phase?: string;
  operation_id?: string;
  elapsed_ms?: number;
}

/** 脚本产物 */
export interface DcScriptArtifact {
  python_path: string;
  python_text: string;
  warnings: string[];
  generated_at: string;
  included_operations: number;
  omitted_operations: Array<{ invocation_id: string; tool: string; reason: string }>;
  /** 脚本是否含显式断言且应用身份非占位值；旧脚本无此字段时按 false 处理 */
  replay_eligible?: boolean;
  /** 成功执行的 assert_* 调用数（replay_eligible 的判定依据之一） */
  explicit_assertions?: number;
}

/** DC 会话蒸馏 Profile 的结果（POST /api/dc/sessions/{id}/profile/distill） */
export interface DcDistillResult {
  profile_id: string;
  status: "draft" | "candidate" | "verified";
  pages_covered: number;
  stable_locators: number;
  assertions: number;
  replay_run_id?: string | null;
  replay_passed?: boolean | null;
  warnings: string[];
}

/** 手动追加 Hypium 回放结果（POST /api/profiles/{id}/replay） */
export interface ProfileReplayResponse {
  profile_id: string;
  results: Array<{
    attempt: number;
    run_id: string | null;
    passed: boolean;
    status: string;
    evidence_paths: string[];
    profile_status: string;
    total_replays: number;
  }>;
  status: string | null;
  total_replays: number;
  max_replays: number;
}

/** 会话内累计 token 用量（与后端 DcTokenUsage 同名同义） */
export interface DcTokenUsage {
  requests: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  details: Record<string, number>;
}

/** 会话视图（GET /api/dc/sessions/{id} 响应） */
export interface DcSessionView {
  session_id: string;
  device_id: string;
  tier: DcToolTier;
  status: string;
  created_at: string;
  turns: DcTurnRecord[];
  invocations: DcToolInvocation[];
  latest_snapshot_path?: string | null;
  script?: DcScriptArtifact | null;
  /** 是否为从磁盘快照恢复的历史会话 */
  restored?: boolean;
  /** 模型上下文还原方式：full=完整消息历史，text=按轮次重建，none=无 */
  restored_context?: "none" | "full" | "text";
  /** 当前执行中的轮次 ID（idle 时为 null） */
  active_turn_id?: string | null;
  /** 最近一轮的公开连续性摘要（取消/中断后用于提示可继续） */
  continuation?: DcContinuationContext | null;
  /** 会话内累计 token 用量（Mock 或旧快照可能为 null） */
  token_usage?: DcTokenUsage | null;
}

/** 会话摘要（GET /api/dc/sessions 列表项；active=false 表示可从磁盘恢复） */
export interface DcSessionSummary {
  session_id: string;
  device_id: string;
  tier: number;
  status: string;
  created_at: string;
  last_active_at?: string;
  turn_count: number;
  invocation_count: number;
  active?: boolean;
  script_available?: boolean;
  restorable?: boolean;
}

/** 创建会话响应 */
export interface CreateSessionResponse {
  session_id: string;
  device_id: string;
  tier: number;
  status: string;
}

/** 发送消息响应 */
export interface SendMessageResponse {
  turn_id: string;
  status: string;
}

/** 聊天消息角色：用户 / 助手最终回复 / 模型思考 / 模型叙述 / 工具卡 */
export type DcMessageRole = "user" | "assistant" | "thinking" | "narration" | "tool";

/** 前端工具卡状态：running 之外统一收敛为 success/failed 视觉 */
export type DcToolCardStatus = "running" | "success" | "failed";

/** 聊天消息（前端聚合用；工具消息以 invocation_id 作 id 以便就地更新） */
export interface DcChatMessage {
  id: string;
  role: DcMessageRole;
  content: string;
  timestamp: string;
  turnId?: string;
  step?: number;
  /* 工具卡专属字段 */
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolResult?: string;
  toolStatus?: DcToolCardStatus;
  durationMs?: number;
  /** 工具调用的权威状态（新契约）；toolStatus 仅用于视觉分档 */
  toolState?: DcToolStatus;
  toolPhase?: string;
  toolErrorCode?: string | null;
  toolEffectStatus?: DcEffectStatus;
  /** 流式草稿标记：仅 message_delta 增量期间为 true，收口/终态后置 false */
  streaming?: boolean;
  /** 流式草稿键（`{turn_id}:m{index}:thinking|text`）：全量事件据此就地收口同一草稿 */
  streamKey?: string;
}

/* ------------------------------------------------------------------------- */
/* 实时操作账本（store 内部使用的视图模型）                                    */
/* ------------------------------------------------------------------------- */

/** 当前活动阶段种类：决定活动区文案前缀与状态色 */
export type DcActivityKind = "capturing_context" | "model" | "tool" | "cancelling" | "attention" | "idle";

/**
 * 当前活动状态；由 SSE 事件驱动，配合本地每秒 tick 推导已耗时。
 * `epoch` 每次活动变化自增，供组件重建 tick 计时器。
 */
export interface DcActivityState {
  kind: DcActivityKind;
  /** 阶段文案，如「正在等待模型」 */
  phaseLabel: string;
  /** 原始阶段标识（后端 phase 值） */
  phase: string;
  /** 调用 ID：工具为 invocation_id，模型为 model_call_id */
  operationId: string;
  turnId: string | null;
  tool: string | null;
  startedAt: string | null;
  lastProgressAt: string | null;
  deadlineAt: string | null;
  /** 后端给出的已耗时（毫秒）；缺失时由 startedAt 推导 */
  elapsedMs: number | null;
  /** 后端给出的剩余预算（毫秒） */
  remainingMs: number | null;
  cancellable: boolean;
  /** 需要人工确认时的原因与处置选项 */
  attentionReason: string | null;
  attentionOptions: string[];
  /** 活动变化计数 */
  epoch: number;
}

/** 实时工具调用行（历史快照与 SSE 增量合并后的视图模型） */
export interface DcLiveToolRecord {
  invocation_id: string;
  turn_id: string;
  tool: string;
  args: Record<string, unknown>;
  status: DcToolStatus;
  phase: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  last_progress_at: string | null;
  deadline_at: string | null;
  effect_status: DcEffectStatus;
  command_id: string | null;
  result_summary: string;
  error: string | null;
  error_code: string | null;
  cancellable: boolean;
  /** 该行的权威状态是否来自实时事件（对账只能补字段，不能回退实时状态） */
  live: boolean;
}

/** 停止按钮两阶段状态 */
export type DcCancelStatus = "idle" | "requested" | "confirming" | "settled";

/** SSE 连接状态（用于空状态与断线提示） */
export type DcLiveConnection = "idle" | "connecting" | "open" | "reconnecting" | "closed";

/** 当前活动区的实时投影（选择器返回值，避免每次读 store 触发重渲染） */
export interface DcActivitySnapshot {
  activity: DcActivityState;
  activityKey: string;
  /** 是否处于忙碌状态（本地实时状态或 session 投影任一为准） */
  busy: boolean;
  cancelStatus: DcCancelStatus;
  cancelHint: string;
}


/** 层级描述（用于 TierPicker）；L2 含 start_app：会话身份只能由显式启动留下 */
export const TIER_DESCRIPTIONS: Record<DcToolTier, string> = {
  1: "L1 · UI 交互：点击、滑动、输入、按键、截图",
  2: "L2 · 观测诊断与启动：UI 层级、日志、前台应用、应用列表、内存、启动应用",
  3: "L3 · 应用管理：停止、安装、卸载、清除数据",
  4: "L4 · 文件操作：推送、拉取、列出设备文件",
  5: "L5 · 受控 Shell：执行白名单命令（破坏性命令被拦截）",
};
