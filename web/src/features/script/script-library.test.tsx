// 脚本库测试：统一列表（Live + 直流）、选中展示源码、按来源启动。

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const listScripts = vi.hoisted(() => vi.fn());
const getScript = vi.hoisted(() => vi.fn());
const runDcScript = vi.hoisted(() => vi.fn());
const startRunReplay = vi.hoisted(() => vi.fn());
const getRun = vi.hoisted(() => vi.fn());
const dcArtifactUrl = vi.hoisted(() =>
  vi.fn((sessionId: string, path: string) => `/api/dc/sessions/${sessionId}/artifacts/${path}`),
);

vi.mock("../../api/scripts", () => ({
  listScripts,
  getScript,
  runDcScript,
  startRunReplay,
  getRun,
}));

vi.mock("../../api/dc-client", () => ({
  closeDcSession: vi.fn(),
  createDcSession: vi.fn(),
  dcArtifactUrl,
  fetchDcScript: vi.fn(),
  generateDcScript: vi.fn(),
  getDcSession: vi.fn(),
  listDcSessions: vi.fn(),
  resumeDcSession: vi.fn(),
  sendDcMessage: vi.fn(),
  setDcTier: vi.fn(),
  stopDcTurn: vi.fn(),
}));

import { ScriptPanel } from "./ScriptPanel";
import { useConsole } from "../../stores/console";
import type { ScriptCatalogEntry } from "../../api/scripts";
import type { ReplayResult } from "../../api/types";

const LIVE_ID = "run-20260101T000000Z-aaaa1111/generated/test_run.py";
const DC_ID = "dc-20260102T000000Z-bbbb2222/generated/dc_test_dc.py";

function entry(partial: Partial<ScriptCatalogEntry> & Pick<ScriptCatalogEntry, "script_id" | "source">): ScriptCatalogEntry {
  return {
    run_id: partial.script_id.split("/")[0],
    filename: partial.script_id.split("/").at(-1) ?? "script.py",
    python_path: `D:/artifacts/${partial.script_id}`,
    replay_eligible: true,
    warnings: [],
    incomplete_reasons: [],
    size_bytes: 100,
    modified_at: "2026-01-01T08:00:00Z",
    ...partial,
  };
}

const LIVE_ENTRY = entry({
  script_id: LIVE_ID,
  source: "run",
  case_id: "live_case",
  included_actions: 7,
  omitted_actions: 2,
});

const DC_ENTRY = entry({
  script_id: DC_ID,
  source: "dc",
  case_id: "dc_case",
  // 已入库的 DC 录制：只有 case_persisted === true 时 case_id 才被当作脚本身份。
  case_persisted: true,
  replay_eligible: false,
  included_actions: 5,
  omitted_actions: 10,
  warnings: ["inv-1: swipe direction inferred as LEFT"],
});

function detailFor(target: ScriptCatalogEntry) {
  return { entry: target, python: `# ${target.case_id}\nprint('hi')\n`, config: {} };
}

function replayResult(attempt = 1, passed = true): ReplayResult {
  return {
    attempt,
    passed,
    status: passed ? "passed" : "failed",
    exit_code: passed ? 0 : 1,
    evidence_paths: [`hypium/attempt-0${attempt}/stdout.log`],
    command: {
      command: "python script.py",
      args: ["python", "script.py"],
      returncode: passed ? 0 : 1,
      stdout: "",
      stderr: "",
      timed_out: false,
      duration_ms: 1200,
    },
  };
}

beforeEach(() => {
  listScripts.mockReset();
  getScript.mockReset();
  runDcScript.mockReset();
  startRunReplay.mockReset();
  getRun.mockReset();
  dcArtifactUrl.mockClear();
  useConsole.setState({ runId: "run-20260101T000000Z-aaaa1111", operation: "idle", attemptCount: 1, error: "" });
  listScripts.mockResolvedValue([DC_ENTRY, LIVE_ENTRY]);
  getScript.mockImplementation((scriptId: string) =>
    Promise.resolve(detailFor(scriptId === DC_ID ? DC_ENTRY : LIVE_ENTRY)),
  );
});

describe("ScriptPanel 脚本库", () => {
  it("列出 Live 与直流两类脚本，并把当前运行的脚本设为默认选中", async () => {
    render(<ScriptPanel />);

    expect(await screen.findByText("live_case")).toBeInTheDocument();
    expect(screen.getByText("dc_case")).toBeInTheDocument();
    expect(screen.getByText("直流")).toBeInTheDocument();
    // DC_ENTRY 没有 confidence 字段 ⇒ 退化为「低置信」徽章；不可执行时额外显示「不可执行」。
    expect(screen.getAllByText("低置信").length).toBeGreaterThan(0);
    expect(screen.getByText("不可执行")).toBeInTheDocument();
    expect(screen.getByText(/2 个脚本/)).toBeInTheDocument();

    // 当前运行的脚本（LIVE_ID 属于 runId）优先选中
    await waitFor(() => expect(getScript).toHaveBeenCalledWith(LIVE_ID));
    await waitFor(() => expect(screen.getByText(/# live_case/)).toBeInTheDocument());
    expect(screen.getAllByText(/live_case/).length).toBeGreaterThan(1); // 列表 + 详情标题
    expect(screen.getByText(/print\('hi'\)/)).toBeInTheDocument();
  });

  it("点击列表项切换到对应脚本源码", async () => {
    render(<ScriptPanel />);
    await waitFor(() => expect(getScript).toHaveBeenCalledWith(LIVE_ID));

    fireEvent.click(screen.getByText("dc_case").closest("button") as HTMLElement);

    await waitFor(() => expect(getScript).toHaveBeenCalledWith(DC_ID));
    await waitFor(() => expect(screen.getByText(/# dc_case/)).toBeInTheDocument());
  });

  it("直流脚本可诊断启动，并展示证据链接", async () => {
    runDcScript.mockResolvedValue({ script_id: DC_ID, session_id: "dc-20260102T000000Z-bbbb2222", results: [replayResult()] });
    render(<ScriptPanel />);
    await waitFor(() => expect(getScript).toHaveBeenCalledWith(LIVE_ID));

    fireEvent.click(screen.getByText("dc_case").closest("button") as HTMLElement);
    const launch = await screen.findByRole("button", { name: /诊断启动/ });
    fireEvent.click(launch);

    await waitFor(() => expect(runDcScript).toHaveBeenCalledWith(DC_ID, 1));
    expect(await screen.findByText("诊断执行结果")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /stdout\.log/ });
    expect(link.getAttribute("href")).toBe(
      "/api/dc/sessions/dc-20260102T000000Z-bbbb2222/artifacts/hypium/attempt-01/stdout.log",
    );
  });

  it("Live 可回放脚本走验收回放并渲染 attempt 结果", async () => {
    startRunReplay.mockResolvedValue({});
    getRun.mockResolvedValue({ replays: [replayResult(1, true)], replay_status: "passed" });
    render(<ScriptPanel />);
    await waitFor(() => expect(getScript).toHaveBeenCalledWith(LIVE_ID));

    const launch = await screen.findByRole("button", { name: /验收回放 1 次/ });
    fireEvent.click(launch);

    await waitFor(() => expect(startRunReplay).toHaveBeenCalledWith("run-20260101T000000Z-aaaa1111", 1));
    expect(await screen.findByText("验收回放结果")).toBeInTheDocument();
    expect(screen.getByText("Attempt 1")).toBeInTheDocument();
    expect(dcArtifactUrl).not.toHaveBeenCalled();
  });

  it("有质量顾虑的脚本仍可执行，只显示质量提示（不让位给阻断告警）", async () => {
    const medium = entry({
      script_id: "run-2/generated/test_run.py",
      source: "run",
      case_id: "medium_case",
      replay_eligible: true,
      confidence: "medium",
      confidence_factors: ["source trace has no successful explicit assertion"],
    });
    listScripts.mockResolvedValue([medium]);
    getScript.mockResolvedValue(detailFor(medium));

    render(<ScriptPanel />);

    const launch = await screen.findByRole("button", { name: /验收回放 1 次/ });
    expect(launch).toBeEnabled();
    expect(screen.getByText("可执行 · 中置信")).toBeInTheDocument();
    expect(screen.getByText(/质量提示（不影响执行）：置信度 中置信/)).toBeInTheDocument();
    expect(screen.getByText("source trace has no successful explicit assertion")).toBeInTheDocument();
    expect(screen.queryByText(/该脚本当前不可执行/)).not.toBeInTheDocument();
  });

  it("物理上不可执行的脚本禁用启动并列出 runnable_blockers", async () => {
    const blocked = entry({
      script_id: "run-2/generated/test_run.py",
      source: "run",
      case_id: "blocked_case",
      replay_eligible: false,
      confidence: "high",
      runnable_blockers: ["script has no replayable action"],
    });
    listScripts.mockResolvedValue([blocked]);
    getScript.mockResolvedValue(detailFor(blocked));

    render(<ScriptPanel />);

    const launch = await screen.findByRole("button", { name: /验收回放 1 次/ });
    expect(launch).toBeDisabled();
    expect(screen.getByText(/该脚本当前不可执行/)).toBeInTheDocument();
    expect(screen.getByText("script has no replayable action")).toBeInTheDocument();
  });

  it("DC 未入库脚本不用悬空的 case_id 冒充用例身份，并提示可用入库接口", async () => {
    const unsaved = entry({
      script_id: "dc-3/generated/dc_test_dc_3.py",
      source: "dc",
      case_id: "dangling_case",
      case_persisted: false,
      replay_eligible: true,
    });
    listScripts.mockResolvedValue([unsaved]);
    getScript.mockResolvedValue(detailFor(unsaved));

    render(<ScriptPanel />);

    // 等详情加载完成，再断言列表标题与详情标题都用文件名。
    await screen.findByText("dc-3/generated/dc_test_dc_3.py");
    const titles = screen.getAllByText("dc_test_dc_3.py", { selector: "strong" });
    expect(titles).toHaveLength(2);
    expect(screen.queryByText("dangling_case")).not.toBeInTheDocument();
    expect(titles[0].getAttribute("title")).toBe("dc_test_dc_3.py");
    expect(screen.getByText(/尚未保存为可复用用例/)).toBeInTheDocument();
    expect(screen.getByText(/POST \/api\/cases\/from-dc\//)).toBeInTheDocument();
  });

  it("DC 已入库脚本（case_persisted=true）仍用 case_id 作为身份并展示用例 ID", async () => {
    const persisted = entry({
      script_id: "dc-4/generated/dc_test_dc_4.py",
      source: "dc",
      case_id: "persisted_case",
      case_persisted: true,
      replay_eligible: true,
    });
    listScripts.mockResolvedValue([persisted]);
    getScript.mockResolvedValue(detailFor(persisted));

    render(<ScriptPanel />);

    await screen.findByText("dc-4/generated/dc_test_dc_4.py");
    // 列表标题 + 详情标题 + 详情里的用例 ID（<code>）
    expect(screen.getAllByText("persisted_case")).toHaveLength(3);
    expect(screen.getByText("persisted_case", { selector: "code" })).toBeInTheDocument();
    expect(screen.queryByText(/尚未保存为可复用用例/)).not.toBeInTheDocument();
  });

  it("旧 DC 产物（缺 case_persisted 键）按未入库处理，不显示悬空 case_id", async () => {
    const legacy = entry({
      script_id: "dc-5/generated/dc_test_dc_5.py",
      source: "dc",
      case_id: "legacy_dangling_case",
      replay_eligible: true,
    });
    listScripts.mockResolvedValue([legacy]);
    getScript.mockResolvedValue(detailFor(legacy));

    render(<ScriptPanel />);

    await screen.findByText("dc-5/generated/dc_test_dc_5.py");
    expect(screen.getAllByText("dc_test_dc_5.py", { selector: "strong" })).toHaveLength(2);
    expect(screen.queryByText("legacy_dangling_case")).not.toBeInTheDocument();
    // 缺该键（未知）不渲染入库提示，避免对旧产物下结论。
    expect(screen.queryByText(/尚未保存为可复用用例/)).not.toBeInTheDocument();
  });

  it("Live 脚本没有 case_persisted 键也照旧用 case_id 作为身份", async () => {
    listScripts.mockResolvedValue([LIVE_ENTRY]);
    getScript.mockResolvedValue(detailFor(LIVE_ENTRY));

    render(<ScriptPanel />);

    await screen.findByText(LIVE_ID);
    // 列表标题 + 详情标题（Live 语义不变）
    expect(screen.getAllByText("live_case")).toHaveLength(2);
    expect(screen.queryByText(/尚未保存为可复用用例/)).not.toBeInTheDocument();
  });
});
