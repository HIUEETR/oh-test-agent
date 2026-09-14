// DC 模式 API 客户端封装。
// 复用 api/client.ts 的 apiUrl 和 apiJson，不修改 client.ts。

import { apiJson, apiUrl } from "./client";
import type {
  CreateSessionResponse,
  DcScriptArtifact,
  DcSessionSummary,
  DcSessionView,
  DcToolTier,
  SendMessageResponse,
} from "./dc-types";

/** 创建 DC 会话 */
export function createDcSession(body?: { device_id?: string; tier?: number }): Promise<CreateSessionResponse> {
  return apiJson<CreateSessionResponse>("/api/dc/sessions", {
    method: "POST",
    body: body ?? {},
  });
}

/** 列出活跃会话摘要 */
export function listDcSessions(): Promise<DcSessionSummary[]> {
  return apiJson<DcSessionSummary[]>("/api/dc/sessions");
}

/** 获取会话全量投影 */
export function getDcSession(sessionId: string): Promise<DcSessionView> {
  return apiJson<DcSessionView>(`/api/dc/sessions/${encodeURIComponent(sessionId)}`);
}

/** 发送用户消息 */
export function sendDcMessage(sessionId: string, text: string): Promise<SendMessageResponse> {
  return apiJson<SendMessageResponse>(`/api/dc/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    body: { text },
  });
}

/** 取消当前 turn */
export function stopDcTurn(sessionId: string): Promise<{ session_id: string; status: string }> {
  return apiJson(`/api/dc/sessions/${encodeURIComponent(sessionId)}/stop`, { method: "POST" });
}

/** 变更工具层级 */
export function setDcTier(sessionId: string, tier: DcToolTier): Promise<{ session_id: string; tier: number }> {
  return apiJson(`/api/dc/sessions/${encodeURIComponent(sessionId)}/tier`, {
    method: "PATCH",
    body: { tier },
  });
}

/** 触发脚本生成 */
export function generateDcScript(
  sessionId: string,
  bundleName?: string,
  mainAbility?: string,
): Promise<DcScriptArtifact> {
  return apiJson<DcScriptArtifact>(`/api/dc/sessions/${encodeURIComponent(sessionId)}/script`, {
    method: "POST",
    body: {
      bundle_name: bundleName ?? "com.example.app",
      main_ability: mainAbility ?? "EntryAbility",
    },
  });
}

/** 获取已生成的脚本 */
export function fetchDcScript(sessionId: string): Promise<DcScriptArtifact> {
  return apiJson<DcScriptArtifact>(`/api/dc/sessions/${encodeURIComponent(sessionId)}/script`);
}

/** 关闭会话 */
export function closeDcSession(sessionId: string): Promise<{ session_id: string; status: string }> {
  return apiJson(`/api/dc/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

/** 构造 DC 产物下载 URL */
export function dcArtifactUrl(sessionId: string, path: string): string {
  return apiUrl(`/api/dc/sessions/${encodeURIComponent(sessionId)}/artifacts/${path}`);
}
