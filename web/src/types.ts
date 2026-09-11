// 本文件描述前端实际消费的 API JSON 子集；字段命名保持后端序列化格式。
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

export type Snapshot = {
  snapshot_id: string;
  image_path: string;
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

export type DiscoveryStatus = {
  phase?: "bootstrap" | "task";
  provisional?: boolean;
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
  replays?: Array<{ attempt: number; passed: boolean }>;
  gates?: Record<string, boolean | number | string>;
};

export type RunTrace = {
  run_id: string;
  target_app_id: string;
  task: string;
  state: string;
  phase?: "bootstrap" | "task";
  provisional?: boolean;
  profile_status_at_start?: string;
  profile_snapshot?: ProfileSummary;
  resolved_target?: TargetCandidate;
  target_candidates?: TargetCandidate[];
  discovery_result?: Record<string, unknown>;
  verification_result?: Record<string, unknown>;
  profile_validation_replays?: Array<{ attempt: number; passed: boolean }>;
  discovery?: DiscoveryStatus;
  mode: string;
  model_used: string;
  model_mock: boolean;
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
    nodes: Array<{
      node_id: string;
      title: string;
      page_path: string;
      snapshot_id: string;
      image_path: string;
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
  generated?: { warnings: string[] };
  replays: Array<{ attempt: number; passed: boolean }>;
};

export type ScriptResult = {
  python: string;
  config: Record<string, unknown>;
  warnings: string[];
};
