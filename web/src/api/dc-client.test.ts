// DC API 客户端契约测试：身份参数必须「未提供就不序列化」。
//
// 回归背景（task-30e 问题 3）：前端曾在 generateDcScript 里补
// `bundleName ?? "com.example.app"`，于是 composer 的脚本 pill 无参调用时发出的是
// **显式占位身份**，后端按「显式身份优先」直接采用，从会话录制推断身份的改动
// 在 UI 路径上永远不生效，脚本里仍是 BUNDLE_NAME='com.example.app'。

import { beforeEach, describe, expect, it, vi } from "vitest";

const apiJson = vi.hoisted(() => vi.fn());
const apiUrl = vi.hoisted(() => vi.fn((path: string) => path));

vi.mock("./client", () => ({ apiJson, apiUrl }));

import { distillDcProfile, generateDcScript } from "./dc-client";

describe("dc-client 身份参数序列化", () => {
  beforeEach(() => {
    apiJson.mockReset();
    apiJson.mockResolvedValue({});
  });

  it("省略身份时不发送占位值，交给后端推断", () => {
    void generateDcScript("dc-1");

    expect(apiJson).toHaveBeenCalledWith("/api/dc/sessions/dc-1/script", { method: "POST", body: {} });
  });

  it("只提供一半身份时只序列化该字段（后端整体回落推断）", () => {
    void generateDcScript("dc-1", "com.example.notes");

    expect(apiJson).toHaveBeenCalledWith("/api/dc/sessions/dc-1/script", {
      method: "POST",
      body: { bundle_name: "com.example.notes" },
    });
  });

  it("显式提供完整身份时原样发送", () => {
    void generateDcScript("dc-1", "com.example.notes", "MainAbility");

    expect(apiJson).toHaveBeenCalledWith("/api/dc/sessions/dc-1/script", {
      method: "POST",
      body: { bundle_name: "com.example.notes", main_ability: "MainAbility" },
    });
  });

  it("蒸馏端点同样不注入占位身份", () => {
    void distillDcProfile("dc-1");

    expect(apiJson).toHaveBeenCalledWith("/api/dc/sessions/dc-1/profile/distill", { method: "POST", body: {} });
  });

  it("蒸馏端点显式身份时原样发送", () => {
    void distillDcProfile("dc-1", "com.example.notes", "MainAbility");

    expect(apiJson).toHaveBeenCalledWith("/api/dc/sessions/dc-1/profile/distill", {
      method: "POST",
      body: { bundle_name: "com.example.notes", main_ability: "MainAbility" },
    });
  });
});
