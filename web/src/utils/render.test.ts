// Markdown 渲染与产物 URL 的单元测试（含 XSS 防护）。

import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdown";
import { artifactUrl, imagePath } from "./artifact";

describe("renderMarkdown", () => {
  it("渲染基础 Markdown 结构", () => {
    const html = renderMarkdown("## 标题\n\n- 项目一\n- 项目二");
    expect(html).toContain("<h2>标题</h2>");
    expect(html).toContain("<li>项目一</li>");
  });

  it("剥离脚本注入（XSS 防护）", () => {
    const html = renderMarkdown('hello <script>alert(1)</script><img src=x onerror="alert(1)">');
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("onerror");
  });

  it("空文本返回空字符串", () => {
    expect(renderMarkdown("")).toBe("");
    expect(renderMarkdown("   ")).toBe("");
  });
});

describe("artifactUrl", () => {
  it("把含运行 ID 的绝对路径归一化为 artifacts API 地址", () => {
    const url = artifactUrl("run-1", String.raw`D:\work\artifacts\runs\run-1\shots\a.png`);
    expect(url).toBe("/api/runs/run-1/artifacts/shots/a.png");
  });

  it("POSIX 路径同样处理且逐段编码", () => {
    const url = artifactUrl("run-2", "/artifacts/runs/run-2/discovery/page 1.png");
    expect(url).toBe("/api/runs/run-2/artifacts/discovery/page%201.png");
  });

  it("空 runId 或空路径返回空串", () => {
    expect(artifactUrl("", "/x.png")).toBe("");
    expect(artifactUrl("run-1", "")).toBe("");
  });
});

describe("imagePath", () => {
  it("优先 artifact_path", () => {
    expect(imagePath({ artifact_path: "/a", image_path: "/b" })).toBe("/a");
    expect(imagePath({ image_path: "/b" })).toBe("/b");
    expect(imagePath({})).toBe("");
  });
});
