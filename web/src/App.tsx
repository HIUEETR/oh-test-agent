import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Activity, Bot, Braces, CircleStop, FileCode2, GitBranch, MonitorSmartphone,
  Play, RefreshCw, ServerCog, ShieldCheck, TerminalSquare,
} from "lucide-react";
import { Background, Controls, ReactFlow, type Edge, type Node } from "@xyflow/react";
import type { Health, RunEvent, RunTrace, ScriptResult } from "./types";

const API = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";
const terminalStates = new Set([
  "completed", "failed_device", "failed_model", "failed_element", "failed_action",
  "failed_assertion", "failed_script", "stopped_by_user",
]);
type Tab = "live" | "graph" | "script" | "report";

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [task, setTask] = useState(
    "打开知乎++，进入搜索，输入 OpenHarmony，返回首页，打开一条内容详情，确认页面存在可见内容后返回首页。",
  );
  const [mode, setMode] = useState("regression");
  const [runId, setRunId] = useState(
    () => new URLSearchParams(window.location.search).get("run_id") ?? "",
  );
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [script, setScript] = useState<ScriptResult | null>(null);
  const [tab, setTab] = useState<Tab>(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    return requested && ["live", "graph", "script", "report"].includes(requested)
      ? requested as Tab
      : "live";
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadHealth = async () => {
    try {
      const response = await fetch(API + "/api/health");
      if (!response.ok) throw new Error("health " + response.status);
      setHealth(await response.json());
    } catch (cause) {
      setError("无法连接后端：" + String(cause));
    }
  };

  useEffect(() => { void loadHealth(); }, []);

  useEffect(() => {
    if (!runId) return;
    const source = new EventSource(API + "/api/runs/" + runId + "/events");
    const eventTypes = [
      "run_started", "preflight_passed", "screen_captured", "elements_detected", "plan_created",
      "action_started", "action_finished", "assertion_passed", "assertion_failed", "page_discovered",
      "edge_created", "script_generated", "execution_started", "execution_finished", "run_failed", "run_finished",
    ];
    const handler = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as RunEvent;
      setEvents((current) => current.some((item) => item.event_id === event.event_id) ? current : [...current, event]);
    };
    eventTypes.forEach((name) => source.addEventListener(name, handler as EventListener));
    source.onerror = () => source.close();
    return () => source.close();
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const response = await fetch(API + "/api/runs/" + runId);
        if (response.ok && !cancelled) {
          const next = (await response.json()) as RunTrace;
          if (next.snapshots) setTrace(next);
          if (!terminalStates.has(next.state)) window.setTimeout(poll, 800);
          else setBusy(false);
        } else if (!cancelled) window.setTimeout(poll, 500);
      } catch (cause) {
        if (!cancelled) setError(String(cause));
      }
    };
    void poll();
    return () => { cancelled = true; };
  }, [runId]);

  const startRun = async () => {
    setBusy(true); setError(""); setEvents([]); setTrace(null); setScript(null); setTab("live");
    try {
      const response = await fetch(API + "/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_app_id: "zhihu-plus", task, mode, max_steps: 20, auto_generate: true }),
      });
      if (!response.ok) throw new Error(await response.text());
      setRunId((await response.json()).run_id);
    } catch (cause) {
      setBusy(false); setError(String(cause));
    }
  };

  const stopRun = async () => {
    if (!runId) return;
    await fetch(API + "/api/runs/" + runId + "/stop", { method: "POST" });
    setBusy(false);
  };

  const loadScript = async () => {
    if (!runId) return;
    const response = await fetch(API + "/api/runs/" + runId + "/script");
    if (response.ok) setScript(await response.json());
  };

  const generate = async () => {
    if (!runId) return;
    setBusy(true);
    const response = await fetch(API + "/api/runs/" + runId + "/generate", { method: "POST" });
    setBusy(false);
    if (!response.ok) return setError(await response.text());
    await loadScript(); setTab("script");
  };

  const execute = async () => {
    if (!runId) return;
    setBusy(true);
    const response = await fetch(API + "/api/runs/" + runId + "/execute?attempts=3", { method: "POST" });
    setBusy(false);
    if (!response.ok) setError(await response.text());
    else {
      const latest = await fetch(API + "/api/runs/" + runId);
      if (latest.ok) setTrace(await latest.json());
    }
  };

  useEffect(() => { if (tab === "script") void loadScript(); }, [tab, runId]);

  const latestSnapshot = trace?.snapshots.at(-1);
  const graph = useMemo(() => toFlow(trace), [trace]);
  const state = trace?.state ?? (runId ? "created" : "idle");

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
                <option value="regression">回归测试</option><option value="exploration">探索测试</option>
                <option value="stability">稳定性测试</option><option value="reproduction">问题复现</option>
              </select></div>
              <div><label>目标应用</label><div className="static-field">知乎++</div></div>
            </div>
            <div className="button-row">
              <button className="primary" onClick={startRun} disabled={busy || !task.trim()}><Play size={17} />启动 Agent</button>
              <button className="danger" onClick={stopRun} disabled={!busy || !runId}><CircleStop size={17} />停止</button>
            </div>
          </section>

          <section className="panel health-panel">
            <div className="section-title"><ServerCog size={18} /><span>运行环境</span>
              <button className="icon-button" onClick={loadHealth} aria-label="刷新环境状态"><RefreshCw size={15} /></button>
            </div>
            <HealthRow icon={<MonitorSmartphone size={16} />} label="HDC Device" value={health?.device.id ?? "unknown"} ok={Boolean(health?.device.connected)} />
            <HealthRow icon={<Bot size={16} />} label="Agent Provider" value={health?.model.provider ?? "unknown"} ok={Boolean(health)} />
            <HealthRow icon={<ShieldCheck size={16} />} label="Hypium" value={health?.hypium.version ?? "not found"} ok={Boolean(health?.hypium.importable)} />
          </section>

          {runId && <section className="panel run-card">
            <p className="eyebrow">CURRENT RUN</p><code>{runId}</code><div className={"state-pill state-" + state}>{state}</div>
            <div className="metrics"><Metric label="步骤" value={trace?.actions.length ?? 0} /><Metric label="页面" value={trace?.graph.nodes.length ?? 0} /><Metric label="断言" value={trace?.assertions.length ?? 0} /></div>
          </section>}
        </aside>

        <section className="main-panel">
          <nav className="tabs" aria-label="运行结果视图">
            <TabButton active={tab === "live"} onClick={() => setTab("live")} icon={<Activity size={16} />} label="实时执行" />
            <TabButton active={tab === "graph"} onClick={() => setTab("graph")} icon={<GitBranch size={16} />} label="页面关系图" />
            <TabButton active={tab === "script"} onClick={() => setTab("script")} icon={<FileCode2 size={16} />} label="生成脚本" />
            <TabButton active={tab === "report"} onClick={() => setTab("report")} icon={<Braces size={16} />} label="报告" />
          </nav>
          {error && <div className="error-banner" role="alert">{error}</div>}

          {tab === "live" && <div className="live-grid">
            <section className="viewport-card">
              <div className="card-heading"><span>设备画面</span><small>{latestSnapshot ? latestSnapshot.width + " × " + latestSnapshot.height : "等待截图"}</small></div>
              <div className="device-stage">{latestSnapshot ? <img src={artifactUrl(runId, latestSnapshot.image_path)} alt="当前 OpenHarmony 设备截图" /> : <EmptyState />}</div>
            </section>
            <section className="timeline-card">
              <div className="card-heading"><span>Agent Trace</span><small>{events.length} events</small></div>
              <div className="timeline" aria-live="polite">{events.length ? [...events].reverse().map((event) => <div className="event" key={event.event_id}>
                <span className={"event-dot event-" + event.type} /><div><strong>{event.message}</strong><small>{event.type} · {new Date(event.timestamp).toLocaleTimeString()}</small></div>
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

          {tab === "graph" && <section className="graph-card">{graph.nodes.length ? <ReactFlow nodes={graph.nodes} edges={graph.edges} fitView minZoom={0.25} maxZoom={1.8}>
            <Background color="#253957" gap={22} /><Controls position="bottom-right" />
          </ReactFlow> : <EmptyState title="页面图尚未生成" />}</section>}

          {tab === "script" && <section className="script-layout">
            <div className="action-strip"><button className="secondary" onClick={generate} disabled={!runId || busy}><FileCode2 size={16} />重新生成</button>
              <button className="primary" onClick={execute} disabled={!script || busy}><Play size={16} />连续回放 3 次</button></div>
            {script?.warnings.length ? <div className="warning-list">{script.warnings.map((warning) => <p key={warning}>{warning}</p>)}</div> : null}
            <pre className="code-view"><code>{script?.python ?? "运行 Agent 后将在这里展示确定性生成的 Hypium Python 用例。"}</code></pre>
          </section>}

          {tab === "report" && <section className="report-layout">
            {runId ? <>
              <div className="report-toolbar">
                <a className="secondary report-download" href={API + "/api/runs/" + runId + "/report?download=true"}>下载 HTML 报告</a>
              </div>
              <div className="report-card"><iframe title="测试运行报告" src={API + "/api/runs/" + runId + "/report"} /></div>
            </> : <section className="report-card"><EmptyState title="尚无可查看的报告" /></section>}
          </section>}
        </section>
      </main>
    </div>
  );
}

function StatusDot({ ok, label }: { ok: boolean; label: string }) { return <span className="status-dot"><i className={ok ? "ok" : "muted"} />{label}</span>; }
function HealthRow({ icon, label, value, ok }: { icon: ReactNode; label: string; value: string; ok: boolean }) { return <div className="health-row"><span className="health-icon">{icon}</span><div><small>{label}</small><strong>{value}</strong></div><i className={ok ? "health-ok" : "health-off"} /></div>; }
function Metric({ label, value }: { label: string; value: number }) { return <div><strong>{String(value).padStart(2, "0")}</strong><small>{label}</small></div>; }
function TabButton({ active, onClick, icon, label }: { active: boolean; onClick: () => void; icon: ReactNode; label: string }) { return <button className={active ? "tab active" : "tab"} onClick={onClick}>{icon}{label}</button>; }
function EmptyState({ title = "等待 Agent 采集设备画面" }: { title?: string }) { return <div className="empty-state"><MonitorSmartphone size={38} /><strong>{title}</strong><span>运行状态和证据会实时出现在这里</span></div>; }
function EmptyLog() { return <div className="empty-log"><Activity size={24} /><span>启动任务后显示规划、工具调用和断言事件</span></div>; }

function artifactUrl(runId: string, absolutePath: string) {
  const normalized = absolutePath.replaceAll("\\", "/");
  const marker = "/" + runId + "/";
  const index = normalized.indexOf(marker);
  const relative = index >= 0 ? normalized.slice(index + marker.length) : normalized.split("/").slice(-2).join("/");
  return API + "/api/runs/" + runId + "/artifacts/" + relative;
}

function toFlow(trace: RunTrace | null): { nodes: Node[]; edges: Edge[] } {
  if (!trace) return { nodes: [], edges: [] };
  const nodes: Node[] = trace.graph.nodes.map((node, index) => ({
    id: node.node_id,
    position: { x: (index % 3) * 290, y: Math.floor(index / 3) * 190 },
    data: { label: <div className="flow-node"><small>STATE {node.discovered_order}</small><strong>{node.title}</strong><span>{node.element_count} elements</span></div> },
    className: "flow-card",
  }));
  const edges: Edge[] = trace.graph.edges.map((edge) => ({
    id: edge.edge_id, source: edge.source, target: edge.target,
    label: edge.target_description ? edge.action + ": " + edge.target_description : edge.action,
    animated: true, style: { stroke: "#35d6a4", strokeWidth: 2 }, labelStyle: { fill: "#a7bad4", fontSize: 11 },
  }));
  return { nodes, edges };
}
