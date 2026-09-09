// 本文件描述前端实际消费的 API JSON 子集；字段命名保持后端序列化格式，避免在网络边界重复映射。
export type Health = {
  status: string;
  model: { configured: boolean; vision_configured: boolean; provider: string };
  device: { connected: boolean; id: string; resolution?: [number, number]; error?: string };
  hypium: { importable: boolean; version?: string };
};

export type RunEvent = {
  // event_id 只保证在同一 run_id 内单调递增，前端据此合并 SSE 历史事件和实时事件。
  event_id: number;
  run_id: string;
  type: string;
  timestamp: string;
  message: string;
  // 不同事件拥有不同负载，读取方必须先根据 type 收窄，不能在边界处假定具体结构。
  payload: Record<string, unknown>;
};

export type Snapshot = {
  snapshot_id: string;
  // 后端持久化绝对文件路径，展示前需由 artifactUrl 转换为当前 Run 的产物接口 URL。
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
  // RunTrace 是轮询接口返回的聚合快照；数组内容会随 Agent 执行持续追加。
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
    // 图节点是已去重的页面状态，不与 snapshots 保持一一对应关系。
    nodes: Array<{
      node_id: string;
      title: string;
      page_path: string;
      image_path: string;
      element_count: number;
      discovered_order: number;
    }>;
    // 边的 source/target 引用上方 node_id，前端转换时必须保留该标识。
    edges: Array<{
      edge_id: string;
      source: string;
      target: string;
      action: string;
      target_description: string;
    }>;
  };
  // 脚本生成前该字段缺省；脚本文本和配置通过独立的 script 接口获取。
  generated?: { warnings: string[] };
  replays: Array<{ attempt: number; passed: boolean }>;
};

export type ScriptResult = {
  python: string;
  // 配置结构由生成器决定，消费方应在读取具体键前执行运行时收窄。
  config: Record<string, unknown>;
  warnings: string[];
};
