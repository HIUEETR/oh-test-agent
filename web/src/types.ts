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

export type RunTrace = {
  run_id: string;
  task: string;
  state: string;
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
