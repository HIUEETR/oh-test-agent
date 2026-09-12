// 本文件描述前端实际消费的 API JSON 子集；可选字段兼容旧 Trace 与并行演进中的后端契约。
export type Health = {
  status: string;
  model: { configured: boolean; vision_configured: boolean; provider: string };
  device: { connected: boolean; id: string; resolution?: [number, number]; error?: string };
  hypium: { importable: boolean; version?: string };
};

export type RunEvent = {
  event_id: number;
  run_id: string;
  type: string;
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
  plan: Array<{ step_id: string; instruction: string; tool: string; target?: string }>;
  snapshots: Snapshot[];
  actions: Array<{
    step_id: string;
    tool: string;
    success: boolean;
    duration_ms: number;
    error?: string;
    warnings: string[];
  }>;
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
    incomplete_reasons?: string[];
  };
  replays: ReplayResult[];
  agent_outcome?: "completed" | "failed" | "stopped" | "unknown";
  agent_error?: string;
  replay_status?: "not_requested" | "not_eligible" | "pending" | "passed" | "failed" | "partial";
  replay_total?: number;
  replay_completed?: number;
  replay_passed?: number;
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
