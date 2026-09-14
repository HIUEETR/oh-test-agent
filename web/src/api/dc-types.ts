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

/** 工具调用录制记录 */
export interface DcToolInvocation {
  invocation_id: string;
  turn_id: string;
  tool: DcToolName;
  tier: DcToolTier;
  args: Record<string, unknown>;
  success: boolean;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  error?: string | null;
  before_snapshot_id?: string | null;
  after_snapshot_id?: string | null;
}

/** 对话轮次状态 */
export type DcTurnStatus = "running" | "completed" | "blocked" | "needs_user" | "failed" | "cancelled";

/** 对话轮次记录 */
export interface DcTurnRecord {
  turn_id: string;
  user_message: string;
  status: DcTurnStatus;
  agent_summary: string;
  started_at: string;
  ended_at?: string | null;
  invocation_ids: string[];
  error?: string | null;
}

/** 事件类型 */
export type DcEventType =
  | "session_created" | "session_closed"
  | "turn_started" | "turn_finished"
  | "tool_call_started" | "tool_call_finished"
  | "screenshot_captured" | "ui_tree_captured"
  | "assistant_message" | "script_generated"
  | "tier_changed" | "error";

/** DC 事件类型列表（用于 SSE 订阅） */
export const DC_EVENT_TYPES: DcEventType[] = [
  "session_created", "session_closed",
  "turn_started", "turn_finished",
  "tool_call_started", "tool_call_finished",
  "screenshot_captured", "ui_tree_captured",
  "assistant_message", "script_generated",
  "tier_changed", "error",
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

/** 脚本产物 */
export interface DcScriptArtifact {
  python_path: string;
  python_text: string;
  warnings: string[];
  generated_at: string;
  included_operations: number;
  omitted_operations: Array<{ invocation_id: string; tool: string; reason: string }>;
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
}

/** 会话摘要（GET /api/dc/sessions 列表项） */
export interface DcSessionSummary {
  session_id: string;
  device_id: string;
  tier: number;
  status: string;
  created_at: string;
  turn_count: number;
  invocation_count: number;
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

/** 聊天消息（前端聚合用） */
export interface DcChatMessage {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  timestamp: string;
  turnId?: string;
  invocations?: DcToolInvocation[];
}

/** 层级描述（用于 TierPicker） */
export const TIER_DESCRIPTIONS: Record<DcToolTier, string> = {
  1: "L1 · UI 交互：点击、滑动、输入、按键、截图",
  2: "L2 · 观测诊断：UI 层级、日志、前台应用、应用列表、内存",
  3: "L3 · 应用管理：启动、停止、安装、卸载、清除数据",
  4: "L4 · 文件操作：推送、拉取、列出设备文件",
  5: "L5 · 受控 Shell：执行白名单命令（破坏性命令被拦截）",
};
