// 统一的 API 访问层：URL 解析、JSON 请求封装、中文错误映射与并发锁。

const configuredApi = String(import.meta.env.VITE_API_URL ?? "").trim().replace(/\/$/, "");
const API_DISPLAY = configuredApi || `${window.location.origin}/api`;

/** 把 API 路径解析为完整 URL；支持通过 VITE_API_URL 指向独立后端。 */
export function apiUrl(path: string): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  if (!configuredApi) return normalized;
  if (configuredApi.endsWith("/api") && normalized.startsWith("/api/")) {
    return configuredApi + normalized.slice(4);
  }
  return configuredApi + normalized;
}

/** 顶部横幅展示用的 API 地址。 */
export function apiDisplay(): string {
  return API_DISPLAY;
}

/** 把任意异常映射为「中文场景前缀：原因」的展示文本。 */
export function apiError(label: string, cause: unknown): string {
  const detail = cause instanceof Error ? cause.message : String(cause);
  return `${label}：${detail}`;
}

/** 从 fetch 响应中提取后端 detail 文本（失败时的中文消息来源）。 */
export async function responseDetail(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
    if (parsed.detail !== undefined) return JSON.stringify(parsed.detail);
  } catch {
    /* 非 JSON 响应按原文返回 */
  }
  return text || `HTTP ${response.status}`;
}

/**
 * JSON 请求封装：自动序列化请求体、解析响应 JSON，
 * 非 2xx 时抛出携带后端 detail 的 Error。
 */
export async function apiJson<T>(
  path: string,
  init?: Omit<RequestInit, "body"> & { body?: unknown },
): Promise<T> {
  const { body, ...rest } = init ?? {};
  const response = await fetch(apiUrl(path), {
    ...rest,
    headers: { "Content-Type": "application/json", ...(rest.headers ?? {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await responseDetail(response));
  return await response.json() as T;
}

/** 简单的命名并发锁：同一操作在请求进行中时不重复发起。 */
export class RequestLocks {
  private readonly held = new Set<string>();

  acquire(key: string): boolean {
    if (this.held.has(key)) return false;
    this.held.add(key);
    return true;
  }

  release(key: string): void {
    this.held.delete(key);
  }
}

export const locks = new RequestLocks();
