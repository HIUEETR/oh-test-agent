// 缺陷面板（Phase 5.2）测试：列表渲染、过滤、详情、生成复现用例、空态。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const listDefects = vi.hoisted(() => vi.fn());
const getDefect = vi.hoisted(() => vi.fn());
const patchDefect = vi.hoisted(() => vi.fn());
const toBugRepro = vi.hoisted(() => vi.fn());

vi.mock("../../api/defects", async () => {
  const actual = await vi.importActual<typeof import("../../api/defects")>("../../api/defects");
  return {
    ...actual,
    listDefects,
    getDefect,
    patchDefect,
    toBugRepro,
  };
});

import type { DefectRecord, DefectSummary } from "../../api/types";
import { DefectDetail, DefectPanel, DefectRow, severityTone, statusTone } from "./DefectPanel";

function summary(overrides: Partial<DefectSummary> = {}): DefectSummary {
  return {
    defect_id: "defect-abc",
    bundle_name: "com.zhihu.hmos",
    kind: "cppcrash",
    severity: "critical",
    status: "suspected",
    title_zh: "hilog 中出现 C++ 崩溃",
    summary_zh: "hilog 中出现 C++ 崩溃（cppcrash）",
    page_path: "pages/Feed",
    action_id: "step-2",
    occurrences: 1,
    first_seen_at: "2026-01-01T08:00:00Z",
    last_seen_at: "2026-01-01T08:05:00Z",
    run_id: "run-1",
    finding_count: 1,
    ...overrides,
  };
}

function record(overrides: Partial<DefectRecord> = {}): DefectRecord {
  return {
    ...summary(),
    findings: [
      {
        kind: "cppcrash",
        severity: "critical",
        summary_zh: "hilog 中出现 C++ 崩溃",
        detail: "cppcrash happened",
        source: "hilog",
        action_id: "step-2",
        page_path: "pages/Feed",
        phase: "in_run",
      },
    ],
    evidence_paths: ["hilog_inrun_step-2.txt"],
    device_id: "SN1",
    ...overrides,
  };
}

beforeEach(() => {
  listDefects.mockReset();
  getDefect.mockReset();
  patchDefect.mockReset();
  toBugRepro.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("DefectPanel", () => {
  it("renders the defect list sorted by severity and shows the critical count", async () => {
    listDefects.mockResolvedValue({
      total: 2,
      defects: [
        summary({ defect_id: "defect-warn", severity: "warning", title_zh: "白屏", kind: "white_screen" }),
        summary({ defect_id: "defect-crit", severity: "critical", title_zh: "崩溃", kind: "cppcrash" }),
      ],
    });
    getDefect.mockResolvedValue(record({ defect_id: "defect-crit" }));

    render(<DefectPanel />);

    await waitFor(() => expect(screen.getByText("崩溃")).toBeInTheDocument());
    const rows = screen.getAllByRole("button").filter((node) => node.className.includes("defect-row"));
    expect(rows[0].textContent).toContain("崩溃");
    expect(screen.getByText(/critical 1/)).toBeInTheDocument();
  });

  it("shows the empty state when there are no defects", async () => {
    listDefects.mockResolvedValue({ total: 0, defects: [] });

    render(<DefectPanel />);

    await waitFor(() => expect(screen.getByText("暂无缺陷")).toBeInTheDocument());
  });

  it("passes the status filter through to the API", async () => {
    listDefects.mockResolvedValue({ total: 0, defects: [] });

    render(<DefectPanel />);
    await waitFor(() => expect(listDefects).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("按状态过滤"), { target: { value: "confirmed" } });

    await waitFor(() =>
      expect(listDefects).toHaveBeenCalledWith(expect.objectContaining({ status: "confirmed" })),
    );
  });

  it("passes runId and bundleName filters to the API", async () => {
    listDefects.mockResolvedValue({ total: 0, defects: [] });

    render(<DefectPanel runId="run-9" bundleName="com.zhihu.hmos" />);

    await waitFor(() =>
      expect(listDefects).toHaveBeenCalledWith(
        expect.objectContaining({ run_id: "run-9", bundle_name: "com.zhihu.hmos" }),
      ),
    );
  });

  it("surfaces a load failure instead of crashing", async () => {
    listDefects.mockRejectedValue(new Error("backend down"));

    render(<DefectPanel />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });

  it("loads the detail of the selected defect", async () => {
    listDefects.mockResolvedValue({ total: 1, defects: [summary()] });
    getDefect.mockResolvedValue(record());

    render(<DefectPanel />);

    await waitFor(() => expect(getDefect).toHaveBeenCalledWith("defect-abc"));
    await waitFor(() => expect(screen.getByText("hilog_inrun_step-2.txt")).toBeInTheDocument());
  });
});

describe("DefectDetail", () => {
  it("renders the evidence links, facts and findings", () => {
    render(<DefectDetail defect={record()} onChanged={() => {}} />);

    expect(screen.getByText("证据链")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /hilog_inrun_step-2\.txt/ });
    expect(link).toHaveAttribute("href", "/api/defects/defect-abc/artifacts/hilog_inrun_step-2.txt");
    expect(screen.getByText("pages/Feed")).toBeInTheDocument();
    expect(screen.getByText(/1 条 finding 明细/)).toBeInTheDocument();
  });

  it("renders the repro case when attached", () => {
    render(<DefectDetail defect={record({ repro_case_id: "case-123" })} onChanged={() => {}} />);

    expect(screen.getByText("复现用例")).toBeInTheDocument();
    expect(screen.getByText("case-123")).toBeInTheDocument();
  });

  it("dismisses the defect through PATCH", async () => {
    patchDefect.mockResolvedValue(record({ status: "dismissed" }));
    const onChanged = vi.fn();
    render(<DefectDetail defect={record()} onChanged={onChanged} />);

    fireEvent.click(screen.getByRole("button", { name: /标记误报/ }));

    await waitFor(() =>
      expect(patchDefect).toHaveBeenCalledWith("defect-abc", expect.objectContaining({ status: "dismissed" })),
    );
    expect(onChanged).toHaveBeenCalled();
  });

  it("generates a repro case and shows the created case id", async () => {
    toBugRepro.mockResolvedValue({ defect_id: "defect-abc", case_id: "case-xyz", execution_id: null });
    const onChanged = vi.fn();
    render(<DefectDetail defect={record()} onChanged={onChanged} />);

    fireEvent.click(screen.getByRole("button", { name: /生成复现用例/ }));

    await waitFor(() => expect(toBugRepro).toHaveBeenCalledWith("defect-abc", true));
    await waitFor(() => expect(screen.getByText(/已生成用例 case-xyz/)).toBeInTheDocument());
    expect(onChanged).toHaveBeenCalled();
  });

  it("keeps the repro button disabled for a dismissed defect", () => {
    render(<DefectDetail defect={record({ status: "dismissed" })} onChanged={() => {}} />);

    expect(screen.getByRole("button", { name: /生成复现用例/ })).toBeDisabled();
  });

  it("reports a backend failure instead of silently swallowing it", async () => {
    toBugRepro.mockRejectedValue(new Error("provider unavailable"));
    render(<DefectDetail defect={record()} onChanged={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /生成复现用例/ }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });

  it("shows an empty state without a defect", () => {
    render(<DefectDetail defect={null} onChanged={() => {}} />);

    expect(screen.getByText("选择一条缺陷")).toBeInTheDocument();
  });
});

describe("tone helpers and row", () => {
  it("maps severities and statuses to badge tones", () => {
    expect(severityTone("critical")).toBe("danger");
    expect(severityTone("warning")).toBe("warn");
    expect(severityTone("info")).toBe("neutral");
    expect(statusTone("confirmed")).toBe("danger");
    expect(statusTone("dismissed")).toBe("ok");
    expect(statusTone("suspected")).toBe("warn");
  });

  it("marks the selected row and calls back with the defect id", () => {
    const onSelect = vi.fn();
    render(<DefectRow defect={summary()} selected onSelect={onSelect} />);

    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("aria-current", "true");
    fireEvent.click(button);
    expect(onSelect).toHaveBeenCalledWith("defect-abc");
  });

  it("shows the occurrence badge only when observed more than once", () => {
    const { rerender } = render(
      <DefectRow defect={summary({ occurrences: 1 })} selected={false} onSelect={() => {}} />,
    );
    expect(screen.queryByText(/×/)).toBeNull();

    rerender(<DefectRow defect={summary({ occurrences: 5 })} selected={false} onSelect={() => {}} />);
    expect(screen.getByText("×5")).toBeInTheDocument();
  });
});
