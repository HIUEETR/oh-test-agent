// DC 模式 API 客户端封装。
// 复用 api/client.ts 的 apiUrl 和 apiJson，不修改 client.ts。

import { apiJson, apiUrl } from "./client";
import type {
  CreateSessionResponse,
  DcDistillResult,
  DcScriptArtifact,
  DcSessionSummary,
  DcSessionView,
  DcToolTier,
  ProfileReplayResponse,
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

/** 从磁盘快照恢复历史会话（服务重启/空闲淘汰/已关闭后仍可继续对话） */
export function resumeDcSession(sessionId: string): Promise<DcSessionView> {
  return apiJson<DcSessionView>(`/api/dc/sessions/${encodeURIComponent(sessionId)}/resume`, {
    method: "POST",
  });
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

/** 组装「可选应用身份」请求体：未提供的字段一律不序列化。
 *
 *  后端把「字段缺省」解释为「请从会话录制推断身份」；前端若补占位值
 *  （com.example.app / EntryAbility），后端会按「显式身份优先」采用，推断永不生效。
 */
function identityBody(
  bundleName?: string,
  mainAbility?: string,
): { bundle_name?: string; main_ability?: string } {
  const body: { bundle_name?: string; main_ability?: string } = {};
  if (bundleName !== undefined) body.bundle_name = bundleName;
  if (mainAbility !== undefined) body.main_ability = mainAbility;
  return body;
}

/** 触发脚本生成。
 *  身份可选：缺省时由后端从会话录制推断。只序列化已提供的字段——
 *  前端补 `?? "com.example.app"` 会让后端按「显式身份优先」采用占位值，推断永不生效。
 */
export function generateDcScript(
  sessionId: string,
  bundleName?: string,
  mainAbility?: string,
): Promise<DcScriptArtifact> {
  return apiJson<DcScriptArtifact>(`/api/dc/sessions/${encodeURIComponent(sessionId)}/script`, {
    method: "POST",
    body: identityBody(bundleName, mainAbility),
  });
}

/** 获取已生成的脚本 */
export function fetchDcScript(sessionId: string): Promise<DcScriptArtifact> {
  return apiJson<DcScriptArtifact>(`/api/dc/sessions/${encodeURIComponent(sessionId)}/script`);
}

/** 从 DC 会话蒸馏 Profile 资产（1 轮设备验证 + 1 次 Hypium 回放）。
 *  身份可选：省略时由后端从会话录制推断，推断失败返回 422 cannot infer。 */
export function distillDcProfile(
  sessionId: string,
  bundleName?: string,
  mainAbility?: string,
): Promise<DcDistillResult> {
  // 未提供的字段不序列化：后端据此区分「显式身份」与「请后端推断」
  return apiJson<DcDistillResult>(`/api/dc/sessions/${encodeURIComponent(sessionId)}/profile/distill`, {
    method: "POST",
    body: identityBody(bundleName, mainAbility),
  });
}

/** 向 candidate/verified Profile 追加 Hypium 回放证据（比赛「3 次连续成功」要求） */
export function replayProfile(profileId: string, attempts: number): Promise<ProfileReplayResponse> {
  return apiJson<ProfileReplayResponse>(`/api/profiles/${encodeURIComponent(profileId)}/replay`, {
    method: "POST",
    body: { attempts },
  });
}

/** 关闭会话 */
export function closeDcSession(sessionId: string): Promise<{ session_id: string; status: string }> {
  return apiJson(`/api/dc/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

/** ``needs_attention`` 的四个处置动作（与后端 ``ResolveAttentionRequest`` 一致）。 */
export type ResolveAction = "reobserve" | "confirm_effect" | "retry" | "terminate";

/** 处置未确认的设备副作用。
 *
 *  历史缺口：``POST /api/dc/sessions/{id}/resolve`` 后端早已实现，前端却从未调用它，
 *  导致 ``needs_attention`` 触发后四个选项只是**纯文本**，用户无法解除阻塞、只能手调 API。
 */
export function resolveSession(sessionId: string, action: ResolveAction): Promise<DcSessionView> {
  return apiJson<DcSessionView>(`/api/dc/sessions/${encodeURIComponent(sessionId)}/resolve`, {
    method: "POST",
    body: { action },
  });
}

/** 构造 DC 产物下载 URL */
export function dcArtifactUrl(sessionId: string, path: string): string {
  return apiUrl(`/api/dc/sessions/${encodeURIComponent(sessionId)}/artifacts/${path}`);
}
