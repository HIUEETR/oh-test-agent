// LLM 文本的 Markdown 渲染：marked 解析 + DOMPurify 消毒，防止模型输出注入 HTML。

import DOMPurify from "dompurify";
import { marked } from "marked";

// 关闭 marked 的 pedantic/异步选项，保证 parse 同步返回字符串。
marked.setOptions({ async: false, gfm: true, breaks: true });

/**
 * 把模型输出的 Markdown/纯文本渲染为安全的 HTML 字符串。
 * 输入先经 marked 解析，再由 DOMPurify 白名单消毒；解析失败时按纯文本转义返回。
 */
export function renderMarkdown(text: string): string {
  if (!text.trim()) return "";
  try {
    const html = marked.parse(text, { async: false });
    return DOMPurify.sanitize(html, { FORBID_TAGS: ["style", "form"], FORBID_ATTR: ["style"] });
  } catch {
    const escaped = text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    return `<p>${escaped}</p>`;
  }
}
