// 缺陷（Phase 3/4）API 客户端：列表 / 详情 / 人工处置 / 证据 URL / 转复现用例。

import { apiUrl, apiJson } from "./client";
import type {
  DefectListResponse,
  DefectQuery,
  DefectRecord,
  DefectReproResponse,
  DefectStatus,
} from "./types";

/** 把查询条件拼成 query string（跳过空值）。 */
function toQuery(query: DefectQuery = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** 列出缺陷（按 ``last_seen_at`` 倒序）。 */
export function listDefects(query: DefectQuery = {}): Promise<DefectListResponse> {
  return apiJson<DefectListResponse>(`/api/defects${toQuery(query)}`);
}

/** 读取一条缺陷的完整记录。 */
export function getDefect(defectId: string): Promise<DefectRecord> {
  return apiJson<DefectRecord>(`/api/defects/${encodeURIComponent(defectId)}`);
}

/** 人工处置：确认 / 误报 + 备注。 */
export function patchDefect(
  defectId: string,
  body: { status?: DefectStatus; notes?: string },
): Promise<DefectRecord> {
  return apiJson<DefectRecord>(`/api/defects/${encodeURIComponent(defectId)}`, {
    method: "PATCH",
    body,
  });
}

/** 某次运行发现的缺陷。 */
export function listRunDefects(runId: string, limit = 100): Promise<DefectListResponse> {
  return apiJson<DefectListResponse>(`/api/runs/${encodeURIComponent(runId)}/defects?limit=${limit}`);
}

/** 某应用（Profile）的全部缺陷。 */
export function listProfileDefects(profileId: string, limit = 200): Promise<DefectListResponse> {
  return apiJson<DefectListResponse>(
    `/api/profiles/${encodeURIComponent(profileId)}/defects?limit=${limit}`,
  );
}

/** 把一条缺陷转成复现用例；``autoExecute`` 为真时后端立即排一次重跑。 */
export function toBugRepro(defectId: string, autoExecute = false): Promise<DefectReproResponse> {
  return apiJson<DefectReproResponse>(
    `/api/defects/${encodeURIComponent(defectId)}/to-bug-repro?auto_execute=${autoExecute ? "true" : "false"}`,
    { method: "POST" },
  );
}

/** 缺陷证据文件 URL（路径穿越防护在后端）。 */
export function defectArtifactUrl(defectId: string, path: string): string {
  return apiUrl(`/api/defects/${encodeURIComponent(defectId)}/artifacts/${path}`);
}

/** 类别徽章的中文标签。 */
export const ANOMALY_KIND_LABELS: Record<string, string> = {
  cppcrash: "C++ 崩溃",
  jscrash: "JS 崩溃",
  appfreeze: "应用冻屏",
  anr: "无响应 (ANR)",
  white_screen: "白屏 / 黑屏",
  page_unresponsive: "页面无响应",
  layout_anomaly: "布局异常",
  memory_growth: "内存增长",
};

/** 缺陷状态的中文标签。 */
export const DEFECT_STATUS_LABELS: Record<DefectStatus, string> = {
  suspected: "待确认",
  confirmed: "已确认",
  not_reproduced: "未复现",
  dismissed: "已忽略",
};
