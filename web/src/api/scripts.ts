// 脚本库 API 封装：统一脚本目录（Live 运行 + 直流会话）与启动动作。
// 复用 api/client.ts 的 apiJson/apiUrl，不修改既有客户端。

import { apiJson } from "./client";
import type { ReplayResult, RunTrace } from "./types";

/** 脚本目录项（GET /api/scripts 列表元素） */
export interface ScriptCatalogEntry {
  script_id: string;
  run_id: string;
  source: "run" | "dc";
  filename: string;
  python_path: string;
  case_id?: string | null;
  bundle_name?: string | null;
  main_ability?: string | null;
  purpose?: string | null;
  /** 脚本是否**可执行**（缺可回放动作或身份占位时为 false）；≠ 质量是否合格 */
  replay_eligible: boolean;
  /** 质量分档：只影响徽章与排序，不阻断执行 */
  confidence?: "high" | "medium" | "low" | null;
  confidence_factors?: string[];
  /** 能否作为 Profile 晋级证据 */
  promotion_eligible?: boolean;
  /** 不可执行的原因（replay_eligible=false 时非空） */
  runnable_blockers?: string[];
  included_actions?: number | null;
  omitted_actions?: number | null;
  warnings: string[];
  incomplete_reasons: string[];
  generated_at?: string | null;
  modified_at?: string | null;
  size_bytes: number;
}

/** 脚本详情（GET /api/scripts/{id} 响应） */
export interface ScriptDetail {
  entry: ScriptCatalogEntry;
  python: string;
  config: Record<string, unknown>;
}

/** 直流脚本诊断启动响应 */
export interface DcScriptRunResponse {
  script_id: string;
  session_id: string;
  results: ReplayResult[];
}

/** 列出全部已生成的 Hypium 脚本（最新在前）。 */
export function listScripts(): Promise<ScriptCatalogEntry[]> {
  return apiJson<ScriptCatalogEntry[]>("/api/scripts");
}

/** 读取单个脚本的源码与配置。 */
export function getScript(scriptId: string): Promise<ScriptDetail> {
  const encoded = scriptId.split("/").filter(Boolean).map(encodeURIComponent).join("/");
  return apiJson<ScriptDetail>(`/api/scripts/${encoded}`);
}

/** 在设备上诊断执行直流录制的脚本（不参与验收结论）。 */
export function runDcScript(scriptId: string, attempts = 1): Promise<DcScriptRunResponse> {
  return apiJson<DcScriptRunResponse>("/api/dc/scripts/run", {
    method: "POST",
    body: { script_id: scriptId, attempts },
  });
}

/** 触发某个 Live 运行的验收回放（后台任务，需轮询 getRun）。 */
export function startRunReplay(runId: string, attempts: number): Promise<unknown> {
  return apiJson(`/api/runs/${encodeURIComponent(runId)}/execute?attempts=${attempts}`, { method: "POST" });
}

/** 读取运行轨迹（用于轮询验收回放进度）。 */
export function getRun(runId: string): Promise<RunTrace> {
  return apiJson<RunTrace>(`/api/runs/${encodeURIComponent(runId)}`);
}
