// 产物（截图/证据/报告）访问地址工具：把后端绝对路径归一化为 artifacts API 相对地址。

import { apiUrl } from "../api/client";

/** 优先 artifact_path，其次 image_path。 */
export function imagePath(item: { artifact_path?: string; image_path?: string }): string {
  return item.artifact_path || item.image_path || "";
}

/**
 * 把后端返回的产物绝对路径（Windows/POSIX）转换成
 * /api/runs/{run_id}/artifacts/{relative} 形式的可访问地址。
 */
export function artifactUrl(runId: string, path: string): string {
  if (!runId || !path) return "";
  const normalized = path.replaceAll("\\", "/");
  const marker = `/${runId}/`;
  const markerIndex = normalized.indexOf(marker);
  let relative = markerIndex >= 0 ? normalized.slice(markerIndex + marker.length) : normalized;
  relative = relative.replace(/^\.?\//, "").replace(/^artifacts\/runs\/[^/]+\//, "");
  // 兜底：仍然像盘符根路径时只保留最后两级目录，避免泄露本地路径结构。
  if (/^[A-Za-z]:\//.test(relative) || relative.startsWith("/")) relative = relative.split("/").slice(-2).join("/");
  const encoded = relative.split("/").filter(Boolean).map((segment) => encodeURIComponent(segment)).join("/");
  return apiUrl(`/api/runs/${encodeURIComponent(runId)}/artifacts/${encoded}`);
}
