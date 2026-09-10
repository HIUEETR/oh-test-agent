import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Activity, Bot, Braces, CircleStop, ExternalLink, FileCode2, GitBranch,
  MonitorSmartphone, Play, RefreshCw, RotateCcw, ServerCog, ShieldCheck, TerminalSquare,
} from "lucide-react";
import PageGraphView from "./components/PageGraphView";
import type { ExecuteResult, Health, ReplayResult, RunEvent, RunTrace, ScriptResult } from "./types";

const configuredApi = String(import.meta.env.VITE_API_URL ?? "").trim().replace(/\/$/, "");
const API_DISPLAY = configuredApi || `${window.location.origin}/api`;
const terminalStates = new Set([
  "completed", "failed_device", "failed_model", "failed_element", "failed_action",
  "failed_assertion", "failed_script", "stopped_by_user",
]);
const eventTypes = [
  "run_started", "preflight_passed", "screen_captured", "elements_detected", "plan_created",
  "action_started", "action_finished", "assertion_passed", "assertion_failed", "page_discovered",
  "edge_created", "script_generated", "execution_started", "execution_finished", "run_failed", "run_finished",
];
type Tab = "live" | "graph" | "script" | "report";
type Operation = "idle" | "generating" | "executing";

function apiUrl(path: string) {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  if (!configuredApi) return normalized;
  if (configuredApi.endsWith("/api") && normalized.startsWith("/api/")) {
    return configuredApi + normalized.slice(4);
  }
  return configuredApi + normalized;
}

function apiError(label: string, cause: unknown) {
  const detail = cause instanceof Error ? cause.message : String(cause);
  return `${label}：${detail}`;
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthUnavailable, setHealthUnavailable] = useState(false);
  const [healthLoading, setHealthLoading] = useState(false);
  const [task, setTask] = useState("打开知乎++，进入搜索，输入 OpenHarmony，返回首页，打开一条内容详情，确认页面存在可见内容后返回首页。");
  const [mode, setMode] = useState("regression");
  const [runId, setRunId] = useState(() => new URLSearchParams(window.location.search).get("run_id") ?? "");
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [script, setScript] = useState<ScriptResult | null>(null);
  const [tab, setTab] = useState<Tab>(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    return requested && ["live", "graph", "script", "report"].includes(requested) ? requested as Tab : "live";
  });
  const [runBusy, setRunBusy] = useState(false);
  const [operation, setOperation] = useState<Operation>("idle");
  const [attemptCount, setAttemptCount] = useState<1 | 3>(3);
  const [executionStartedAt, setExecutionStartedAt] = useState<number | null>(null);
  const [clock, setClock] = useState(Date.now());
  const [error, setError] = useState("");
  const [reportReady, setReportReady] = useState(false);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportRevision, setReportRevision] = useState(0);
  const locks = useRef(new Set<string>());

  const loadHealth = useCallback(async () => {
    if (locks.current.has("health")) return;
    locks.current.add("health");
    setHealthLoading(true);
    try {
      const response = await fetch(apiUrl("/api/health"));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setHealth(await response.json() as Health);
      setHealthUnavailable(false);
      setError("");
    } catch (cause) {
      setHealth(null);
      setHealthUnavailable(true);
      setError(apiError("API 不可用", cause));
    } finally {
      setHealthLoading(false);
      locks.current.delete("health");
    }
  }, []);

  useEffect(() => { void loadHealth(); }, [loadHealth]);

  useEffect(() => {
    if (!runId) return;
    const source = new EventSource(apiUrl(`/api/runs/${encodeURIComponent(runId)}/events`));
    const handler = (message: MessageEvent) => {
      try {
        const event = JSON.parse(String(message.data)) as RunEvent;
        setEvents((current) => current.some((item) => item.event_id === event.event_id) ? current : [...current, event]);
      } catch (cause) {
        setError(apiError("无法解析运行事件", cause));
      }
    };
    eventTypes.forEach((name) => source.addEventListener(name, handler as EventListener));
    source.onerror = () => source.close();
    return () => source.close();
  }, [runId]);

  const refreshTrace = useCallback(async (id = runId, quiet = false) => {
    if (!id) return null;
    try {
      const response = await fetch(apiUrl(`/api/runs/${encodeURIComponent(id)}`));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = await response.json() as RunTrace;
      if (next.snapshots) setTrace(next);
      return next;
    } catch (cause) {
      if (!quiet) setError(apiError("无法刷新运行状态", cause));
      return null;
    } finally {
      // 调用方负责下一轮调度，确保任何请求结局都不会形成重叠轮询。
    }
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      try {
        const next = await refreshTrace(runId, true);
        if (!cancelled && next && terminalStates.has(next.state)) setRunBusy(false);
        if (!cancelled && (!next || !terminalStates.has(next.state) || next.replay_status === "pending" || operation === "executing")) {
          timer = window.setTimeout(poll, next ? 900 : 550);
        }
      } catch (cause) {
        if (!cancelled) setError(apiError("运行轮询失败", cause));
      } finally {
        // timer 在成功和失败路径中均由上方串行安排。
      }
    };
    void poll();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [runId, operation, refreshTrace]);

  useEffect(() => {
    if (operation !== "executing") return;
    const timer = window.setInterval(() => setClock(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [operation]);

  const startRun = async () => {
    if (locks.current.has("start")) return;
    locks.current.add("start");
    setRunBusy(true); setError(""); setEvents([]); setTrace(null); setScript(null); setTab("live");
    try {
      const response = await fetch(apiUrl("/api/runs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_app_id: "zhihu-plus", task, mode, max_steps: 20, auto_generate: true }),
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json() as { run_id: string };
      setRunId(result.run_id);
    } catch (cause) {
      setError(apiError("启动 Agent 失败", cause));
      setRunBusy(false);
    } finally {
      locks.current.delete("start");
    }
  };

  const stopRun = async () => {
    if (!runId || locks.current.has("stop")) return;
    locks.current.add("stop");
    try {
      const response = await fetch(apiUrl(`/api/runs/${encodeURIComponent(runId)}/stop`), { method: "POST" });
      if (!response.ok) throw new Error(await response.text());
    } catch (cause) {
      setError(apiError("停止运行失败", cause));
    } finally {
      setRunBusy(false);
      locks.current.delete("stop");
    }
  };

  const loadScript = useCallback(async (quiet = false) => {
    if (!runId) return;
    try {
      const response = await fetch(apiUrl(`/api/runs/${encodeURIComponent(runId)}/script`));
      if (response.status === 404) { setScript(null); return; }
      if (!response.ok) throw new Error(await response.text());
      setScript(await response.json() as ScriptResult);
    } catch (cause) {
      if (!quiet) setError(apiError("读取 Hypium 脚本失败", cause));
    } finally {
      // 读取结束后不保留独立忙碌状态。
    }
  }, [runId]);

  const generate = async () => {
    if (!runId || locks.current.has("generate")) return;
    locks.current.add("generate");
    setOperation("generating"); setError("");
    try {
      const response = await fetch(apiUrl(`/api/runs/${encodeURIComponent(runId)}/generate`), { method: "POST" });
      if (!response.ok) throw new Error(await response.text());
      await loadScript();
      await refreshTrace(runId, true);
      setReportRevision((value) => value + 1);
      setTab("script");
    } catch (cause) {
      setError(apiError("生成 Hypium 脚本失败", cause));
    } finally {
      setOperation("idle");
      locks.current.delete("generate");
    }
  };

  const execute = async () => {
    if (!runId || locks.current.has("execute")) return;
    locks.current.add("execute");
    setOperation("executing"); setExecutionStartedAt(Date.now()); setClock(Date.now()); setError("");
    try {
      const response = await fetch(apiUrl(`/api/runs/${encodeURIComponent(runId)}/execute?attempts=${attemptCount}`), { method: "POST" });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json() as ExecuteResult;
      if (result.replays) setTrace((current) => current ? { ...current, replays: result.replays ?? current.replays } : current);
      let next = await refreshTrace(runId, true);
      while (next?.replay_status === "pending") {
        await new Promise((resolve) => window.setTimeout(resolve, 900));
        next = await refreshTrace(runId, true);
      }
      setReportRevision((value) => value + 1);
    } catch (cause) {
      setError(apiError("Hypium 验收回放失败", cause));
    } finally {
      setOperation("idle");
      setExecutionStartedAt(null);
      locks.current.delete("execute");
    }
  };

  useEffect(() => { if (tab === "script") void loadScript(true); }, [tab, runId, loadScript]);

  const traceRevision = trace?.revision ?? trace?.updated_at ?? `${trace?.state ?? "none"}-${trace?.replays.length ?? 0}-${trace?.generated ? 1 : 0}`;
  const probeReport = useCallback(async () => {
    if (!runId || locks.current.has("report")) return;
    locks.current.add("report");
    setReportLoading(true); setReportReady(false);
    try {
      const response = await fetch(apiUrl(`/api/runs/${encodeURIComponent(runId)}/report?revision=${encodeURIComponent(String(traceRevision))}`));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await response.text();
      setReportReady(true);
    } catch (cause) {
      setError(apiError("报告暂不可用", cause));
    } finally {
      setReportLoading(false);
      locks.current.delete("report");
    }
  }, [runId, traceRevision]);

  useEffect(() => { if (tab === "report" && runId) void probeReport(); }, [tab, runId, reportRevision, probeReport]);

  const latestSnapshot = trace?.snapshots.at(-1);
  const graphArtifactUrl = useCallback((path: string) => artifactUrl(runId, path), [runId]);
  const state = trace?.state ?? (runId ? "created" : "idle");
  const scriptDiagnostic = script?.diagnostic ?? (script?.purpose ? script.purpose === "diagnostic" : !["regression", "stability"].includes(trace?.mode ?? mode));
  const replayAllowed = script?.acceptance_replay_enabled ?? !scriptDiagnostic;
  const scriptStatus = operation === "generating" ? "生成中" : script ? (scriptDiagnostic ? "诊断脚本" : "已生成") : trace?.generated ? "正在读取" : "待自动生成";
  const operationBusy = operation !== "idle";
  const completedAttempts = trace?.replay_completed ?? trace?.replays.filter((item) => replayStatus(item) === "passed" || replayStatus(item) === "failed").length ?? 0;
  const progress = operation === "executing" ? Math.min(95, Math.max(8, (completedAttempts / attemptCount) * 100)) : trace?.replays.length ? 100 : 0;
  const reportUrl = apiUrl(`/api/runs/${encodeURIComponent(runId)}/report?revision=${encodeURIComponent(`${traceRevision}-${reportRevision}`)}`);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark"><Bot size={21} aria-hidden="true" /></div>
        <div><p className="eyebrow">OPENHARMONY QUALITY LAB</p><h1>Multimodal Test Agent</h1></div>
        <div className="topbar-status">
          <StatusDot ok={Boolean(health?.device.connected)} label={health?.device.connected ? "设备在线" : "设备离线"} />
          <StatusDot ok={Boolean(health?.model.configured)} label={health?.model.configured ? "VLM 已配置" : "Mock 模式"} />
        </div>
      </header>

      <main className="workspace">
        <aside className="control-panel">
          <section className="panel intro-panel">
            <div className="section-title"><TerminalSquare size={18} /><span>任务控制</span></div>
            <label htmlFor="task">自然语言测试目标</label>
            <textarea id="task" value={task} onChange={(event) => setTask(event.target.value)} rows={7} />
            <div className="field-row">
              <div><label htmlFor="mode">运行模式</label><select id="mode" value={mode} onChange={(event) => setMode(event.target.value)}>
                <option value="regression">回归测试</option><option value="exploration">探索诊断</option>
                <option value="stability">稳定性测试</option><option value="reproduction">问题诊断</option>
              </select></div>
              <div><label>目标应用</label><div className="static-field">知乎++</div></div>
            </div>
            <div className="button-row">
              <button className="primary" onClick={startRun} disabled={runBusy || operationBusy || !task.trim()}><Play size={17} />启动 Agent</button>
              <button className="danger" onClick={stopRun} disabled={!runBusy || !runId}><CircleStop size={17} />停止</button>
            </div>
          </section>

          <section className="panel health-panel">
            <div className="section-title"><ServerCog size={18} /><span>运行环境</span>
              <button className="icon-button" onClick={loadHealth} disabled={healthLoading} aria-label="刷新环境状态"><RefreshCw size={15} /></button>
            </div>
            <HealthRow icon={<MonitorSmartphone size={16} />} label="HDC Device" value={health?.device.id ?? "unknown"} ok={Boolean(health?.device.connected)} />
            <HealthRow icon={<Bot size={16} />} label="Agent Provider" value={health?.model.provider ?? "unknown"} ok={Boolean(health)} />
            <HealthRow icon={<ShieldCheck size={16} />} label="Hypium" value={health?.hypium.version ?? "not found"} ok={Boolean(health?.hypium.importable)} />
          </section>

          {runId && <section className="panel run-card">
            <p className="eyebrow">CURRENT RUN</p><code>{runId}</code><div className={`state-pill state-${state}`}>{state}</div>
            <div className="metrics"><Metric label="步骤" value={trace?.actions.length ?? 0} /><Metric label="页面" value={trace?.graph.nodes.length ?? 0} /><Metric label="断言" value={trace?.assertions.length ?? 0} /></div>
          </section>}
        </aside>

        <section className="main-panel">
          <nav className="tabs" aria-label="运行结果视图">
            <TabButton active={tab === "live"} onClick={() => setTab("live")} icon={<Activity size={16} />} label="实时执行" />
            <TabButton active={tab === "graph"} onClick={() => setTab("graph")} icon={<GitBranch size={16} />} label="页面关系图" />
            <TabButton active={tab === "script"} onClick={() => setTab("script")} icon={<FileCode2 size={16} />} label="Hypium 脚本" />
            <TabButton active={tab === "report"} onClick={() => setTab("report")} icon={<Braces size={16} />} label="报告" />
          </nav>
          {healthUnavailable && <div className="api-unavailable" role="alert">
            <strong>API 服务不可用</strong><span>当前 API：<code>{API_DISPLAY}</code></span><span>请在项目根目录运行 <code>uv run main.py</code>，然后重试。</span>
            <button className="secondary" onClick={loadHealth} disabled={healthLoading}><RotateCcw size={15} />重试连接</button>
          </div>}
          {error && <div className="error-banner" role="alert">{error}</div>}

          {tab === "live" && <div className="live-grid">
            <section className="viewport-card">
              <div className="card-heading"><span>设备画面</span><small>{latestSnapshot ? `${latestSnapshot.width} × ${latestSnapshot.height}` : "等待截图"}</small></div>
              <div className="device-stage">{latestSnapshot ? <RetryImage src={artifactUrl(runId, imagePath(latestSnapshot))} alt="当前 OpenHarmony 设备截图" /> : <EmptyState />}</div>
            </section>
            <section className="timeline-card">
              <div className="card-heading"><span>Agent Trace</span><small>{events.length} events</small></div>
              <div className="timeline" aria-live="polite">{events.length ? [...events].reverse().map((event) => <div className="event" key={event.event_id}>
                <span className={`event-dot event-${event.type}`} /><div><strong>{event.message}</strong><small>{event.type} · {new Date(event.timestamp).toLocaleTimeString()}</small></div>
              </div>) : <EmptyLog />}</div>
            </section>
            <section className="elements-card">
              <div className="card-heading"><span>当前元素</span><small>{latestSnapshot?.elements.length ?? 0} detected</small></div>
              <div className="element-table">{latestSnapshot?.elements.slice(0, 20).map((element) => <div className="element-row" key={element.element_id}>
                <span className="element-type">{element.type || "Node"}</span><div><strong>{element.content || element.key || element.id}</strong><small>{element.key || element.id || element.source}</small></div>
                <span className={element.clickable || element.editable ? "tag active" : "tag"}>{element.editable ? "input" : element.clickable ? "click" : "read"}</span>
              </div>)}</div>
            </section>
          </div>}

          {tab === "graph" && <section className="graph-card">
            {trace?.graph.nodes.length ? <PageGraphView runId={runId} graph={trace.graph} artifactUrl={graphArtifactUrl} /> : <EmptyState title="页面图尚未生成" />}
          </section>}

          {tab === "script" && <section className="script-layout">
            <div className="script-heading">
              <div><p className="eyebrow">DETERMINISTIC OUTPUT</p><h2>Hypium Python 脚本</h2><span className={`script-status ${scriptDiagnostic ? "diagnostic" : ""}`}>{scriptStatus}</span></div>
              <div className="action-strip">
                <button className="secondary" onClick={generate} disabled={!runId || operationBusy}><FileCode2 size={16} />{script ? "重新生成" : "生成脚本"}</button>
                <label className="attempt-select" htmlFor="attempts">回放次数<select id="attempts" value={attemptCount} onChange={(event) => setAttemptCount(Number(event.target.value) as 1 | 3)} disabled={operationBusy}><option value={1}>1 次</option><option value={3}>3 次</option></select></label>
                <button className="primary" onClick={execute} disabled={!script || operationBusy || !replayAllowed} title={!replayAllowed ? "诊断脚本不能用于验收回放" : undefined}><Play size={16} />验收回放 {attemptCount} 次</button>
              </div>
            </div>
            <p className="generation-note">Agent 运行成功后会自动生成脚本；也可以在此手动生成或重新生成。诊断脚本仅供排查，验收回放已禁用。</p>
            {!replayAllowed && <div className="warning-list" role="alert">当前为诊断脚本，不能作为 Hypium 验收回放证据。</div>}
            {script?.warnings.length ? <div className="warning-list" role="alert">{script.warnings.map((warning) => <p key={warning}>{warning}</p>)}</div> : null}
            {(operation === "executing" || Boolean(trace?.replays.length)) && <ReplayProgress replays={trace?.replays ?? []} attempts={attemptCount} running={operation === "executing"} progress={progress} elapsedMs={executionStartedAt ? clock - executionStartedAt : 0} runId={runId} />}
            <pre className="code-view"><code>{script?.python ?? "运行 Agent 后将在这里展示自动生成的 Hypium Python 用例。"}</code></pre>
          </section>}

          {tab === "report" && <section className="report-layout">
            {runId ? <>
              <div className="report-toolbar">
                <button className="secondary" onClick={probeReport} disabled={reportLoading}><RefreshCw size={15} />重试报告</button>
                {reportReady && <a className="secondary report-download" href={`${reportUrl}&download=true`}>下载 HTML 报告</a>}
              </div>
              <div className="report-card">{reportLoading ? <EmptyState title="正在探测报告" /> : reportReady ? <iframe key={reportUrl} title="测试运行报告" src={reportUrl} /> : <ReportUnavailable onRetry={probeReport} />}</div>
            </> : <section className="report-card"><EmptyState title="尚无可查看的报告" /></section>}
          </section>}
        </section>
      </main>
    </div>
  );
}

function ReplayProgress({ replays, attempts, running, progress, elapsedMs, runId }: { replays: ReplayResult[]; attempts: 1 | 3; running: boolean; progress: number; elapsedMs: number; runId: string }) {
  return <section className="replay-progress" aria-live="polite" aria-busy={running}>
    <div className="progress-heading"><strong>异步回放进度</strong><span>{running ? `执行中 · ${formatDuration(elapsedMs)}` : `${replays.filter((item) => item.passed).length}/${replays.length || attempts} 通过`}</span></div>
    <div className="progress-track"><i style={{ width: `${progress}%` }} /></div>
    <div className="attempt-grid">{Array.from({ length: attempts }, (_, index) => {
      const attempt = replays.find((item) => item.attempt === index + 1);
      const status = attempt ? replayStatus(attempt) : running && index === replays.length ? "running" : "queued";
      const duration = attempt?.command.duration_ms;
      const exitCode = attempt?.exit_code ?? attempt?.command.returncode;
      const failure = attempt?.error?.message || attempt?.command.stderr || (attempt?.timed_out ? "执行超时" : "");
      const evidence = attempt ? replayEvidence(attempt) : [];
      return <article className={`attempt-card attempt-${status}`} key={index}>
        <div><strong>Attempt {index + 1}</strong><span className="attempt-status">{statusLabel(status)}</span></div>
        <dl><div><dt>耗时</dt><dd>{duration === undefined ? "—" : formatDuration(duration)}</dd></div><div><dt>退出码</dt><dd>{exitCode === undefined || exitCode === null ? "—" : exitCode}</dd></div></dl>
        {failure && <p className="attempt-error" role="alert">{failure}</p>}
        {evidence.length > 0 && <div className="evidence-links">{evidence.map((item, evidenceIndex) => <a href={artifactUrl(runId, item.path)} target="_blank" rel="noreferrer" key={`${item.path}-${evidenceIndex}`}><ExternalLink size={12} />{item.label}</a>)}</div>}
      </article>;
    })}</div>
  </section>;
}

function replayEvidence(attempt: ReplayResult) {
  return attempt.evidence_paths.map((path) => ({ label: evidenceLabel(path), path }));
}

function replayStatus(attempt: ReplayResult) {
  if (attempt.status) return attempt.status.toLowerCase();
  return attempt.passed ? "passed" : "failed";
}
function statusLabel(status: string) { return ({ queued: "等待", pending: "等待", running: "执行中", passed: "通过", failed: "失败" } as Record<string, string>)[status] ?? status; }
function evidenceLabel(path: string) { const name = path.replaceAll("\\", "/").split("/").at(-1); return name || "证据"; }
function formatDuration(milliseconds: number) { return milliseconds >= 1000 ? `${(milliseconds / 1000).toFixed(1)} s` : `${Math.max(0, Math.round(milliseconds))} ms`; }
function imagePath(item: { artifact_path?: string; image_path?: string }) { return item.artifact_path || item.image_path || ""; }
function StatusDot({ ok, label }: { ok: boolean; label: string }) { return <span className="status-dot"><i className={ok ? "ok" : "muted"} />{label}</span>; }
function HealthRow({ icon, label, value, ok }: { icon: ReactNode; label: string; value: string; ok: boolean }) { return <div className="health-row"><span className="health-icon">{icon}</span><div><small>{label}</small><strong>{value}</strong></div><i className={ok ? "health-ok" : "health-off"} /></div>; }
function Metric({ label, value }: { label: string; value: number }) { return <div><strong>{String(value).padStart(2, "0")}</strong><small>{label}</small></div>; }
function TabButton({ active, onClick, icon, label }: { active: boolean; onClick: () => void; icon: ReactNode; label: string }) { return <button className={active ? "tab active" : "tab"} onClick={onClick} aria-current={active ? "page" : undefined}>{icon}{label}</button>; }
function EmptyState({ title = "等待 Agent 采集设备画面" }: { title?: string }) { return <div className="empty-state"><MonitorSmartphone size={38} /><strong>{title}</strong><span>运行状态和证据会实时出现在这里</span></div>; }
function EmptyLog() { return <div className="empty-log"><Activity size={24} /><span>启动任务后显示规划、工具调用和断言事件</span></div>; }
function ReportUnavailable({ onRetry }: { onRetry: () => void }) { return <div className="empty-state" role="alert"><Braces size={38} /><strong>报告尚未就绪</strong><span>生成或回放完成后可以再次探测。</span><button className="secondary" onClick={onRetry}><RefreshCw size={15} />重试报告</button></div>; }

function RetryImage({ src, alt }: { src: string; alt: string }) {
  const [revision, setRevision] = useState(0);
  const [failed, setFailed] = useState(false);
  useEffect(() => { setFailed(false); setRevision(0); }, [src]);
  const retry = () => { setFailed(false); setRevision((value) => value + 1); };
  if (!src || failed) return <div className="image-retry" role="alert"><span>图片加载失败</span><button className="secondary" onClick={retry}><RefreshCw size={14} />重试图片</button></div>;
  return <img key={`${src}-${revision}`} src={`${src}${src.includes("?") ? "&" : "?"}revision=${revision}`} alt={alt} onError={() => setFailed(true)} />;
}

function artifactUrl(runId: string, path: string) {
  if (!runId || !path) return "";
  const normalized = path.replaceAll("\\", "/");
  const marker = `/${runId}/`;
  const markerIndex = normalized.indexOf(marker);
  let relative = markerIndex >= 0 ? normalized.slice(markerIndex + marker.length) : normalized;
  relative = relative.replace(/^\.?\//, "").replace(/^artifacts\/runs\/[^/]+\//, "");
  if (/^[A-Za-z]:\//.test(relative) || relative.startsWith("/")) relative = relative.split("/").slice(-2).join("/");
  const encoded = relative.split("/").filter(Boolean).map((segment) => encodeURIComponent(segment)).join("/");
  return apiUrl(`/api/runs/${encodeURIComponent(runId)}/artifacts/${encoded}`);
}
