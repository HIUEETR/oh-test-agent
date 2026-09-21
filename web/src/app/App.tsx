// 应用壳：顶栏（含环境芯片组）、闭环流水线、左栏（启动器/当前运行）与主舞台（标签页）。

import { useEffect, useState, type ReactNode } from "react";
import {
  Activity, AlertTriangle, Bot, Braces, BrainCircuit, FileCode2, GitBranch, History, RotateCcw, ShieldCheck, Zap,
} from "lucide-react";
import clsx from "clsx";
import { apiDisplay } from "../api/client";
import { Badge, StatusDot } from "../components/ui/primitives";
import { useConsole } from "../stores/console";
import { useRunRuntime } from "./use-run-runtime";
import { LauncherPanel } from "../features/launcher/LauncherPanel";
import { TopbarHealth } from "../features/health/TopbarHealth";
import { CurrentRunCard } from "../features/runs/CurrentRunCard";
import { PipelineStrip } from "../features/pipeline/PipelineStrip";
import { DeviceScreen } from "../features/live/DeviceScreen";
import { ThoughtStream } from "../features/live/ThoughtStream";
import { ElementTable } from "../features/live/ElementTable";
import { EventTimeline, LiveViewToggle } from "../features/live/EventTimeline";
import { AdvisorPanel } from "../features/advisor/AdvisorPanel";
import { PageGraphView } from "../features/graph/PageGraphView";
import { ScriptPanel } from "../features/script/ScriptPanel";
import { ProfilesPanel } from "../features/profiles/ProfilesPanel";
import { ReportPanel } from "../features/report/ReportPanel";
import { RunsView } from "../features/runs/RunsView";
import { DcPanel } from "../features/dc/DcPanel";
import { DcOperationLog } from "../features/dc/DcOperationLog";
import { DefectPanel } from "../features/defects/DefectPanel";
import { useDcRuntime } from "../features/dc/use-dc-runtime";
import type { TabKey } from "./deep-links";

const TABS: Array<{ key: TabKey; label: string; icon: ReactNode }> = [
  // 2026-09-17 重构：8 Tab 收敛为 5 Tab（会话 / 页面关系图 / 脚本与回放 / Profile 资产 / 历史运行）。
  // 会话面以 DC（唯一交互入口）为主，同时保留 Live 资产流水线降级视图（在历史运行详情中打开）。
  { key: "session", label: "会话", icon: <Zap size={15} /> },
  { key: "graph", label: "页面关系图", icon: <GitBranch size={15} /> },
  { key: "script", label: "脚本与回放", icon: <FileCode2 size={15} /> },
  { key: "profiles", label: "Profile 资产", icon: <ShieldCheck size={15} /> },
  { key: "runs", label: "历史运行", icon: <History size={15} /> },
  // Phase 5：缺陷一等产物面板（运行中 / 事后发现的异常 + 一键转复现用例）。
  { key: "defects", label: "缺陷", icon: <AlertTriangle size={15} /> },
];

export default function App() {
  useRunRuntime();
  // DC SSE 事件流提升到 App：左栏操作日志因此在任意 Tab（含「脚本与回放」等非会话 Tab）下保持实时。
  useDcRuntime();
  const [liveMode, setLiveMode] = useState<"thoughts" | "events">("thoughts");

  const health = useConsole((state) => state.health);
  const healthUnavailable = useConsole((state) => state.healthUnavailable);
  const healthLoading = useConsole((state) => state.healthLoading);
  const error = useConsole((state) => state.error);
  const tab = useConsole((state) => state.tab);
  const setTab = useConsole((state) => state.setTab);
  const loadHealth = useConsole((state) => state.loadHealth);
  const candidates = useConsole((state) => state.candidates);
  // 「缺陷」Tab 默认只展示当前运行发现的缺陷（无当前运行时展示全部）。
  const currentRunId = useConsole((state) => state.runId);
  const selectedCandidate = useConsole((state) => state.selectedCandidate);
  const patchForm = useConsole((state) => state.patchForm);
  const submitCandidate = useConsole((state) => state.submitCandidate);

  // 首次挂载拉取环境健康与 Profile 列表。
  useEffect(() => {
    void loadHealth();
  }, [loadHealth]);

  const mockModel = Boolean(health && !health.model.configured);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark"><Bot size={20} aria-hidden="true" /></div>
        <div>
          <h1>Multimodal Test Agent</h1>
          <p className="subtitle">OpenHarmony 多模态智能测试控制台</p>
        </div>
        <TopbarHealth />
        <div className="topbar-status">
          <StatusDot ok={Boolean(health?.device.connected)} label={health?.device.connected ? "设备在线" : "设备离线"} />
          <StatusDot ok={Boolean(health?.model.configured)} label={health?.model.configured ? "VLM 已配置" : "Mock 模式"} mock />
        </div>
      </header>

      <main className="workspace">
        <aside className="rail">
          <LauncherPanel />
          <DcOperationLog />
          <CurrentRunCard />
        </aside>

        <section className="stage">
          <PipelineStrip />
          <nav className="panel tab-nav" aria-label="运行结果视图">
            {TABS.map((item) => (
              <button
                type="button"
                key={item.key}
                className={clsx(tab === item.key && "active")}
                onClick={() => setTab(item.key)}
                aria-current={tab === item.key ? "page" : undefined}
              >
                {item.icon}{item.label}
              </button>
            ))}
          </nav>

          {healthUnavailable && (
            <div className="banner info-banner" role="alert">
              <div className="banner-body">
                <strong>API 服务不可用</strong>
                <span>当前 API：<code>{apiDisplay()}</code>，请在项目根目录运行 <code>uv run main.py</code> 后重试。</span>
              </div>
              <button type="button" className="secondary compact" onClick={loadHealth} disabled={healthLoading}>
                <RotateCcw size={14} />重试连接
              </button>
            </div>
          )}
          {error && <div className="banner error-banner" role="alert">{error}</div>}

          {candidates.length > 1 && (
            <div className="banner selection-banner">
              <div className="banner-body">
                <strong>发现多个同名应用</strong>
                <span>请选择目标，系统不会自动猜测。</span>
              </div>
              <select
                value={selectedCandidate}
                onChange={(event) => patchForm({ selectedCandidate: event.target.value })}
                aria-label="选择目标应用"
              >
                <option value="">选择应用</option>
                {candidates.map((candidate) => (
                  <option
                    key={candidate.candidate_id ?? candidate.bundle_name}
                    value={candidate.candidate_id ?? candidate.bundle_name}
                  >
                    {candidate.display_name ?? candidate.app_name ?? "未知应用"} · {candidate.bundle_name}
                  </option>
                ))}
              </select>
              <button type="button" className="primary compact" onClick={submitCandidate} disabled={!selectedCandidate}>
                确认目标
              </button>
            </div>
          )}

          {tab === "session" && <SessionTab liveMode={liveMode} onLiveModeChange={setLiveMode} mockModel={mockModel} />}

          {tab === "graph" && (
            <section className="panel">
              <GraphTabContent />
            </section>
          )}

          {tab === "script" && <ScriptTab />}

          {tab === "profiles" && <ProfilesPanel />}

          {tab === "runs" && <RunsView />}

          {tab === "defects" && <DefectPanel runId={currentRunId || undefined} />}
        </section>
      </main>
    </div>
  );
}

/**
 * 「会话」Tab：默认呈现 DC 模式（唯一交互入口）。
 * Live Mode 的执行可视化（设备屏/元素表/思考流/事件时间线）作为「资产流水线降级」
 * 子视图保留在同一 Tab 内，不再单独占用一个顶层 Tab。
 */
function SessionTab({
  liveMode,
  onLiveModeChange,
  mockModel,
}: {
  liveMode: "thoughts" | "events";
  onLiveModeChange: (mode: "thoughts" | "events") => void;
  mockModel: boolean;
}) {
  const [surface, setSurface] = useState<"dc" | "live" | "advisor">("dc");

  return (
    <div className="session-tab">
      <nav className="panel tab-nav sub-tab-nav" aria-label="会话视图切换">
        <button
          type="button"
          className={clsx(surface === "dc" && "active")}
          onClick={() => setSurface("dc")}
          aria-current={surface === "dc" ? "page" : undefined}
        >
          <Zap size={15} />直流模式（推荐）
        </button>
        <button
          type="button"
          className={clsx(surface === "live" && "active")}
          onClick={() => setSurface("live")}
          aria-current={surface === "live" ? "page" : undefined}
        >
          <Activity size={15} />资产流水线降级
        </button>
        <button
          type="button"
          className={clsx(surface === "advisor" && "active")}
          onClick={() => setSurface("advisor")}
          aria-current={surface === "advisor" ? "page" : undefined}
        >
          <BrainCircuit size={15} />顾问对话
        </button>
      </nav>

      {surface === "dc" && <DcPanel />}

      {surface === "advisor" && <AdvisorPanel />}

      {surface === "live" && (
        <div className="live-grid">
          <div className="live-main">
            <section className="panel">
              <div className="card-heading">
                <span>Agent 思考过程</span>
                {mockModel && <Badge tone="warn">Mock 模型</Badge>}
                <small><LiveViewToggle mode={liveMode} onChange={onLiveModeChange} /></small>
              </div>
              {liveMode === "thoughts" ? <ThoughtStream /> : <EventTimeline />}
            </section>
          </div>
          <div className="live-side">
            <DeviceScreen />
            <ElementTable />
          </div>
        </div>
      )}
    </div>
  );
}

/** 「脚本与回放」Tab：Hypium 脚本列表 + 运行报告子视图。 */
function ScriptTab() {
  const [surface, setSurface] = useState<"script" | "report">("script");

  return (
    <div className="script-tab">
      <nav className="panel tab-nav sub-tab-nav" aria-label="脚本与回放视图切换">
        <button
          type="button"
          className={clsx(surface === "script" && "active")}
          onClick={() => setSurface("script")}
          aria-current={surface === "script" ? "page" : undefined}
        >
          <FileCode2 size={15} />Hypium 脚本
        </button>
        <button
          type="button"
          className={clsx(surface === "report" && "active")}
          onClick={() => setSurface("report")}
          aria-current={surface === "report" ? "page" : undefined}
        >
          <Braces size={15} />运行报告
        </button>
      </nav>
      {surface === "script" ? <ScriptPanel /> : <ReportPanel />}
    </div>
  );
}

/** 图标签内容：优先任务阶段 trace.graph，探索阶段回退 discovery 页面图。 */
function GraphTabContent() {
  const runId = useConsole((state) => state.runId);
  const graph = useConsole((state) => state.trace?.graph ?? null);
  const discovery = useConsole((state) => state.discovery);
  if (!graph && !discovery?.pages?.length) {
    return <div className="panel-pad"><p style={{ color: "var(--text-3)", textAlign: "center" }}>尚无运行数据，页面图随探索运行实时生成。</p></div>;
  }
  return <PageGraphView runId={runId} graph={graph ?? { nodes: [], edges: [] }} discovery={discovery} />;
}
