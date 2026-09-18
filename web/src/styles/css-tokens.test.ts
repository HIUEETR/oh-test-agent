// 令牌回归测试：global.css 引用的每个 var(--x) 必须在 tokens.css 中定义。
// 背景：`.dc-script-dialog { background: var(--surface) }` 曾引用未定义令牌，
// 声明失效导致弹窗透明、遮罩暗色透出整屏发暗。此测试防止同类「用了没定义」再次发生。
//
// 实现说明：只能按文件路径读盘，不能用 `?raw` / import.meta.glob / `?inline`
// —— vitest 的 CSS 管线会把它们一并清空（实测长度为 0）。目录取自
// import.meta.dirname（vitest 下解析为源文件真实目录），Node 类型来自
// src/types/node-shims.d.ts。

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const DIR = import.meta.dirname;

function css(name: string): string {
  return readFileSync(resolve(DIR, name), "utf8");
}

describe("design tokens", () => {
  it("样式源码可读取（防止读取方式失效导致测试空转）", () => {
    expect(css("global.css").length).toBeGreaterThan(10_000);
    expect(css("tokens.css")).toContain("--surface");
  });

  it("global.css 引用的令牌全部在 tokens.css 中定义", () => {
    const used = new Set([...css("global.css").matchAll(/var\(--([a-z0-9-]+)/g)].map((m) => m[1]));
    const defined = new Set([...css("tokens.css").matchAll(/^\s*--([a-z0-9-]+)\s*:/gm)].map((m) => m[1]));
    const missing = [...used].filter((name) => !defined.has(name));
    expect(missing).toEqual([]);
  });

  it("模态弹窗表面令牌必须不透明", () => {
    const surface = /^\s*--surface\s*:\s*([^;]+);/m.exec(css("tokens.css"))?.[1].trim() ?? "";
    expect(surface).not.toBe("");
    expect(surface).not.toMatch(/^rgba\(/);
  });
});
