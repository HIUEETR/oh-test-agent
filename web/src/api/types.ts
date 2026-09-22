// 本文件描述前端消费的后端 API JSON 契约（含本次新增的 LLM 思考留痕字段）。
// 可选字段兼容旧 Trace 与并行演进中的后端契约；新增字段均为后端纯增量输出。

export type Health = {
  status: string;
  model: { configured: boolean; vision_configured: boolean; provider: string };
  device: { connected: boolean; id: string; resolution?: [number, number]; error?: string };
  hypium: { importable: boolean; version?: string };
};

export type RunEventType = string;

export type RunEvent = {
  event_id: number;
  run_id: string;
  type: RunEventType;
  timestamp: string;
  message: string;
  payload: Record<string, unknown>;
};

export type ArtifactImage = {
  artifact_path?: string;
  image_path?: string;
};

export type Snapshot = ArtifactImage & {
  snapshot_id: string;
  width: number;
  height: number;
  page_path: string;
  summary: string;
  elements: Array<{
    element_id: string;
    content: string;
    type: string;
    key: string;
    id: string;
    clickable: boolean;
    editable: boolean;
    source: string;
  }>;
};

export type TargetCandidate = {
  candidate_id?: string;
  target_app_id?: string;
  lifecycle?: string;
  display_name?: string;
  app_name?: string;
  bundle_name: string;
  main_ability?: string;
  version_name?: string;
  version_code?: string | number;
};

export type ProfileSummary = {
  profile_id: string;
  target_app_id?: string;
  lifecycle?: string;
  display_name?: string;
  bundle_name?: string;
  main_ability?: string;
  version_name?: string;
  version_code?: string | number;
  status: "draft" | "candidate" | "verified" | "invalid" | string;
  locked: boolean;
  quick_verification?: { passed?: boolean; checked_at?: string; reason?: string };
  history?: Array<{ backup_name: string; created_at?: string }>;
  /** 已累计的 Hypium 回放证据 ID（比赛「3 次连续成功」进度） */
  hypium_replay_run_ids?: string[];
  /** 回放证据上限（默认 3） */
  max_replays?: number;
  /** 连续通过的回放次数（审计证据里的 consecutive_replay_passes） */
  consecutive_replay_passes?: number;
  /** 门禁 Hypium 脚本路径（存在时可追加异步回放） */
  generated_script_path?: string | null;
};

export type DiscoveryPolicy = {
  enabled: boolean;
  allow_login: boolean;
  allow_permission: boolean;
  allow_submit: boolean;
  allow_publish: boolean;
  allow_download: boolean;
  max_pages: number;
  max_actions_per_page: number;
  max_duration_seconds: number;
  temporary_test: boolean;
};

export type ReplayError = {
  kind: string;
  message: string;
  details?: Record<string, unknown>;
};

export type ReplayResult = {
  attempt: number;
  passed: boolean;
  status: "passed" | "failed" | "timed_out" | "ineligible" | "invalid_result" | string;
  exit_code?: number | null;
  timed_out?: boolean;
  error?: ReplayError | null;
  report_path?: string | null;
  evidence_paths: string[];
  generated_result_path?: string | null;
  generated_result?: Record<string, unknown> | null;
  command: {
    command: string;
    args: string[];
    returncode?: number | null;
    stdout: string;
    stderr: string;
    timed_out: boolean;
    duration_ms: number;
  };
};

/* ---------- LLM 思考留痕（后端增量字段） ---------- */

/** 顾问建议的结构化输出（编号对应候选摘要 candidates 的下标）。 */
export type AdvisorVerdictView = {
  page_summary: string;
  recommended: number[];
  avoid: number[];
  reason: string;
};

/** 候选动作的可读摘要：recommended/avoid 的编号即此列表下标。 */
export type CandidateDigestEntry = {
  index: number;
  kind: string;
  label: string;
  coordinate: [number, number] | null;
};

/** 一次顾问 LLM 调用的输入/输出留痕（AdvisorTurnRecord）。 */
export type AdvisorTurnRecord = {
  turn: number;
  page_path: string;
  snapshot_path: string | null;
  input: string;
  output: AdvisorVerdictView | null;
  source: "model" | "heuristic-fallback" | "error" | string;
  error: string | null;
  elapsed_ms: number | null;
};

/** 按页面归档的顾问结论（advisor_verdicts 列表项）。 */
export type AdvisorVerdictEntry = AdvisorVerdictView & {
  identity: string;
  source: string;
  candidates?: CandidateDigestEntry[];
};

/* ---------- 运行状态 ---------- */

/** 探索发现的页面（DiscoveryResult.pages 列表项）。 */
export type DiscoveryPageView = {
  page_id: string;
  page_path: string;
  snapshot_id: string;
  image_path?: string;
  element_count: number;
  discovered_order: number;
};

/** 探索执行的跳转（DiscoveryResult.transitions 列表项，action 为探索动作摘要）。 */
export type DiscoveryTransitionView = {
  source_page_id: string;
  target_page_id: string | null;
  success: boolean;
  blocked_reason?: string | null;
  action: { kind: string; target_text?: string; locator_value?: string; locator_kind?: string };
};

export type DiscoveryStatus = {
  phase?: "bootstrap" | "task";
  provisional?: boolean;
  live_mode?: boolean;
  profile_status?: string;
  profile_status_at_start?: string;
  profile_snapshot?: ProfileSummary;
  resolved_target?: TargetCandidate;
  target_candidates?: TargetCandidate[];
  pages_discovered?: number;
  actions_executed?: number;
  remaining_seconds?: number;
  blocked_paths?: Array<string | { reason?: string; label?: string; risk_reason?: string; target_text?: string }>;
  locator_candidates?: unknown[];
  assertion_candidates?: unknown[];
  validation_rounds?: Array<{ round_number: number; passed: boolean; failures?: string[] }>;
  replays?: ReplayResult[];
  gates?: Record<string, boolean | number | string>;
  /* DiscoveryResult 展开字段（advisor 相关为本次增量） */
  pages?: DiscoveryPageView[];
  transitions?: DiscoveryTransitionView[];
  advisor_turns?: number;
  advisor_verdicts?: AdvisorVerdictEntry[];
  advisor_log?: AdvisorTurnRecord[];
};

export type PlannedStepView = {
  step_id: string;
  instruction: string;
  tool: string;
  target?: string | null;
  text?: string | null;
  coordinate?: [number, number] | null;
  direction?: string | null;
  wait_seconds?: number | null;
  expected?: string | null;
};

/** 模型逐步工具决策（action_started 事件 payload.decision / ToolDecision）。 */
export type ToolDecisionView = {
  tool: string;
  target?: string | null;
  text?: string | null;
  coordinate?: [number, number] | null;
  direction?: string | null;
  wait_seconds?: number | null;
  reasoning?: string;
};

/** 工具执行结果（action_finished 事件 payload / ActionResult）。 */
export type ActionResultView = {
  step_id: string;
  tool: string;
  success: boolean;
  duration_ms?: number;
  error?: string | null;
  warnings?: string[];
};

export type RunTrace = {
  run_id: string;
  target_app_id: string;
  task: string;
  state: string;
  mode: string;
  model_used: string;
  model_mock: boolean;
  revision?: string | number;
  updated_at?: string;
  phase?: "bootstrap" | "task";
  provisional?: boolean;
  live_mode?: boolean;
  profile_status_at_start?: string;
  profile_snapshot?: ProfileSummary;
  resolved_target?: TargetCandidate;
  target_candidates?: TargetCandidate[];
  discovery_result?: Record<string, unknown>;
  verification_result?: Record<string, unknown>;
  profile_validation_replays?: ReplayResult[];
  discovery?: DiscoveryStatus;
  error?: string;
  plan: PlannedStepView[];
  snapshots: Snapshot[];
  actions: ActionResultView[];
  assertions: Array<{ kind: string; target: string; passed: boolean; message: string }>;
  graph: {
    nodes: Array<ArtifactImage & {
      node_id: string;
      title: string;
      page_path: string;
      snapshot_id: string;
      element_count: number;
      discovered_order: number;
    }>;
    edges: Array<{
      edge_id: string;
      source: string;
      target: string;
      action: string;
      target_description: string;
    }>;
  };
  generated?: {
    warnings: string[];
    generated_at?: string;
    purpose?: string;
    diagnostic?: boolean;
    replay_eligible?: boolean;
    confidence?: "high" | "medium" | "low";
    confidence_factors?: string[];
    promotion_eligible?: boolean;
    promotion_blockers?: string[];
    runnable_blockers?: string[];
    incomplete_reasons?: string[];
  };
  replays: ReplayResult[];
  // Phase 2/3：运行中即时发现的异常，以及靠恢复循环绕路完成的步骤数（additive）。
  defects?: AnomalyFinding[];
  workaround_count?: number;
  analysis?: ExecutionAnalysisView | null;
  agent_outcome?: "completed" | "failed" | "stopped" | "unknown";
  agent_error?: string;
  replay_status?: "not_requested" | "not_eligible" | "pending" | "passed" | "failed" | "partial";
  replay_total?: number;
  replay_completed?: number;
  replay_passed?: number;
};

/** 历史运行摘要（GET /api/runs 列表项）。 */
export type RunSummary = {
  run_id: string;
  state: string;
  target_app_id?: string;
  task?: string;
  updated_at?: string;
};

export type ScriptResult = {
  python: string;
  config: Record<string, unknown>;
  warnings: string[];
  python_path?: string;
  config_path?: string;
  generated_at?: string;
  auto_generated?: boolean;
  purpose?: "acceptance" | "diagnostic" | string;
  diagnostic?: boolean;
  acceptance_replay_enabled?: boolean;
  confidence?: "high" | "medium" | "low";
  confidence_factors?: string[];
  promotion_eligible?: boolean;
  promotion_blockers?: string[];
  runnable_blockers?: string[];
  source_agent_outcome?: string;
  source_action_count?: number;
  included_action_count?: number;
  incomplete_reasons?: string[];
};

export type ExecuteResult = {
  run_id?: string;
  passed?: boolean;
  status?: string;
  attempts?: number;
  replays?: ReplayResult[];
};

/** 后端 EventType 枚举的全量镜像：SSE 订阅按名字监听。 */
export const RUN_EVENT_TYPES = [
  "run_started", "target_candidates_found", "target_resolved", "target_started", "profile_found",
  "profile_revalidation_started", "profile_revalidation_finished", "discovery_started", "discovery_progress",
  "discovery_finished", "discovery_path_blocked", "locator_candidate_observed", "profile_live_mode",
  "profile_incremental", "profile_harvested",
  "profile_draft_saved", "profile_verification_started", "profile_verification_round_started",
  "profile_verification_round_finished", "hypium_replay_started", "hypium_replay_finished", "profile_promoted",
  "original_task_started", "preflight_passed", "screen_captured", "elements_detected", "plan_created",
  "action_started", "action_finished", "assertion_passed", "assertion_failed", "page_discovered",
  "edge_created", "script_generated", "execution_started", "execution_finished", "run_failed", "run_finished",
  // Phase 2/3：运行中异常与缺陷一等产物（后端 EventType.ANOMALY_DETECTED / DEFECT_RECORDED）。
  "anomaly_detected", "defect_recorded",
] as const;

/** 终态集合：与后端 TERMINAL_STATES 保持一致。 */
export const TERMINAL_STATES = new Set([
  "completed", "failed_device", "failed_model", "failed_element", "failed_action",
  "failed_assertion", "failed_script", "failed_target_resolution", "failed_target_probe",
  "failed_discovery", "failed_profile_verification", "failed_profile_promotion", "stopped_by_user",
]);

// ---------------------------------------------------------------------------
// 执行结果分析 / 缺陷（Phase 2/3）：与后端 AnomalyFinding / DefectRecord 对齐
// ---------------------------------------------------------------------------

/** 异常类别：与后端 ``AnomalyKind`` 逐一对应。 */
export type AnomalyKind =
  | "cppcrash" | "jscrash" | "appfreeze" | "anr"
  | "white_screen" | "page_unresponsive" | "layout_anomaly" | "memory_growth";

export type AnomalySeverity = "info" | "warning" | "critical";

/** 发现阶段：区分「事后分析」与「运行中即时发现」。 */
export type AnomalyPhase = "post_hoc" | "in_run" | "exploration" | "replay";

/** 单条异常发现；仅作附加信息，永不翻转用例的 passed。 */
export type AnomalyFinding = {
  kind: AnomalyKind;
  severity: AnomalySeverity;
  summary_zh: string;
  detail?: string;
  evidence?: Record<string, unknown>;
  source: string;
  defect_id?: string;
  action_id?: string;
  page_path?: string;
  screenshot?: string;
  detected_at?: string;
  phase?: AnomalyPhase;
  repro_hint?: string;
};

/** 缺陷生命周期状态：与后端 ``DefectStatus`` 逐一对应。 */
export type DefectStatus = "suspected" | "confirmed" | "not_reproduced" | "dismissed";

/** 列表投影（不含完整 findings）。 */
export type DefectSummary = {
  defect_id: string;
  bundle_name: string;
  kind: AnomalyKind;
  severity: AnomalySeverity;
  status: DefectStatus;
  title_zh: string;
  summary_zh?: string;
  page_path?: string;
  action_id?: string;
  occurrences: number;
  first_seen_at: string;
  last_seen_at: string;
  run_id?: string;
  session_id?: string;
  case_id?: string;
  repro_case_id?: string | null;
  finding_count?: number;
};

/** 完整缺陷记录。 */
export type DefectRecord = DefectSummary & {
  schema_version?: number;
  findings?: AnomalyFinding[];
  evidence_paths?: string[];
  device_id?: string;
  app_version?: string;
  repro_execution_id?: string | null;
  notes?: string;
};

/** ``GET /api/defects`` / ``/api/runs/{id}/defects`` 的响应。 */
export type DefectListResponse = {
  total: number;
  defects: DefectSummary[];
};

/** ``POST /api/defects/{id}/to-bug-repro`` 的响应。 */
export type DefectReproResponse = {
  defect_id: string;
  case_id?: string | null;
  execution_id?: string | null;
  symptom_kind?: string;
  case?: unknown;
};

/** 执行结果分析（后端 ``ExecutionAnalysis``）。 */
export type ExecutionAnalysisView = {
  schema_version?: number;
  subject: string;
  subject_id: string;
  bundle_name?: string;
  device_id?: string;
  healthy: boolean;
  findings: AnomalyFinding[];
  symptom_reproduced?: boolean | null;
  metrics?: Record<string, unknown>;
  log_coverage?: "full" | "partial" | "unavailable";
  analyzed_at?: string;
};

/** 缺陷列表查询条件（全部可选）。 */
export type DefectQuery = {
  bundle_name?: string;
  kind?: AnomalyKind;
  severity?: AnomalySeverity;
  status?: DefectStatus;
  run_id?: string;
  session_id?: string;
  case_id?: string;
  limit?: number;
};
