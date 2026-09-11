import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Activity, Bot, Braces, CircleStop, FileCode2, GitBranch, Lock, MonitorSmartphone,
  Play, RefreshCw, Search, ServerCog, ShieldCheck, TerminalSquare, Unlock,
} from "lucide-react";
import PageGraphView from "./components/PageGraphView";
import type {
  DiscoveryPolicy, DiscoveryStatus, Health, ProfileSummary, RunEvent, RunTrace,
  ScriptResult, TargetCandidate,
} from "./types";

const API = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";
const terminalStates = new Set([
  "completed", "failed_device", "failed_model", "failed_element", "failed_action",
  "failed_assertion", "failed_script", "failed_target_resolution", "failed_target_probe",
  "failed_discovery", "failed_profile_verification", "failed_profile_promotion", "stopped_by_user",
]);
const eventTypes = [
  "run_started", "target_candidates_found", "target_resolved", "target_started", "profile_found",
  "profile_revalidation_started", "profile_revalidation_finished", "discovery_started", "discovery_progress",
  "discovery_finished", "discovery_path_blocked", "locator_candidate_observed", "profile_draft_saved",
  "profile_verification_round_finished", "hypium_replay_finished", "profile_promoted", "original_task_started",
  "preflight_passed", "screen_captured", "elements_detected", "plan_created", "action_started",
  "action_finished", "assertion_passed", "assertion_failed", "page_discovered", "edge_created",
  "script_generated", "execution_started", "execution_finished", "run_failed", "run_finished",
];
type Tab = "live" | "graph" | "script" | "profiles" | "report";
type TargetKind = "app_name" | "bundle_name";

const defaultPolicy: DiscoveryPolicy = {
  enabled: true, allow_login: false, allow_permission: false, allow_submit: false,
  allow_publish: false, allow_download: false, max_pages: 20,
  max_actions_per_page: 8, max_duration_seconds: 900, temporary_test: false,
};

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [targetKind, setTargetKind] = useState<TargetKind>("app_name");
  const [targetValue, setTargetValue] = useState("");
  const [task, setTask] = useState("");
  const [mode, setMode] = useState("regression");
  const [policy, setPolicy] = useState(defaultPolicy);
  const [runId, setRunId] = useState(() => new URLSearchParams(window.location.search).get("run_id") ?? "");
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [discovery, setDiscovery] = useState<DiscoveryStatus | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [script, setScript] = useState<ScriptResult | null>(null);
  const [profiles, setProfiles] = useState<ProfileSummary[]>([]);
  const [candidates, setCandidates] = useState<TargetCandidate[]>([]);
  const [selectedCandidate, setSelectedCandidate] = useState("");
  const [tab, setTab] = useState<Tab>(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    return requested && ["live", "graph", "script", "profiles", "report"].includes(requested)
      ? requested as Tab : "live";
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadHealth = async () => {
    try {
      const response = await fetch(API + "/api/health");
      if (!response.ok) throw new Error("health " + response.status);
      setHealth(await response.json());
    } catch (cause) { setError("无法连接后端：" + String(cause)); }
  };
  const loadProfiles = async () => {
    const response = await fetch(API + "/api/profiles");
    if (response.ok) setProfiles(await response.json());
  };
  useEffect(() => { void loadHealth(); void loadProfiles(); }, []);

  useEffect(() => {
    if (!runId) return;
    const source = new EventSource(API + "/api/runs/" + runId + "/events");
    const handler = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as RunEvent;
      setEvents((current) => current.some((item) => item.event_id === event.event_id) ? current : [...current, event]);
      if (event.type === "target_candidates_found") {
        const next = event.payload.candidates;
        if (Array.isArray(next)) setCandidates(next as TargetCandidate[]);
      }
    };
    eventTypes.forEach((name) => source.addEventListener(name, handler as EventListener));
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) source.close();
    };
    return () => source.close();
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const [traceResponse, discoveryResponse] = await Promise.all([
          fetch(API + "/api/runs/" + runId), fetch(API + "/api/runs/" + runId + "/discovery"),
        ]);
        if (traceResponse.ok && !cancelled) {
          const next = (await traceResponse.json()) as RunTrace;
          if (next.snapshots) setTrace(next);
          if (next.target_candidates?.length) setCandidates(next.target_candidates);
          if (!terminalStates.has(next.state)) window.setTimeout(poll, 800);
          else { setBusy(false); void loadProfiles(); }
        } else if (!cancelled && traceResponse.status === 404) {
          setBusy(false);
          setError("运行不存在或已经被清理");
        } else if (!cancelled) window.setTimeout(poll, 500);
        if (discoveryResponse.ok && !cancelled) {
          const nextDiscovery = (await discoveryResponse.json()) as DiscoveryStatus;
          setDiscovery(nextDiscovery);
          if (nextDiscovery.target_candidates?.length) setCandidates(nextDiscovery.target_candidates);
        }
      } catch (cause) { if (!cancelled) setError(String(cause)); }
    };
    void poll();
    return () => { cancelled = true; };
  }, [runId]);

  const resolveTarget = async () => {
    if (!targetValue.trim()) return;
    setBusy(true); setError("");
    try {
      const response = await fetch(API + "/api/targets/resolve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: { [targetKind]: targetValue.trim() } }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? response.statusText);
      const nextCandidates = (result.candidates ?? []) as TargetCandidate[];
      setCandidates(nextCandidates);
      if (result.target?.bundle_name && targetKind === "bundle_name") setTargetValue(result.target.bundle_name);
      if (result.status === "not_found") setError("未找到匹配的已安装应用");
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };

  const startRun = async () => {
    if (!targetValue.trim()) return;
    setBusy(true); setError(""); setEvents([]); setTrace(null); setDiscovery(null); setScript(null); setCandidates([]); setTab("live");
    try {
      const response = await fetch(API + "/api/runs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target: { [targetKind]: targetValue.trim() },
          task: task.trim() || undefined,
          mode, max_steps: 20, auto_generate: true, discovery: policy,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      setRunId((await response.json()).run_id);
    } catch (cause) { setBusy(false); setError(String(cause)); }
  };

  const submitCandidate = async () => {
    if (!runId || !selectedCandidate) return;
    const candidate = candidates.find((item) => (item.candidate_id ?? item.bundle_name) === selectedCandidate);
    if (!candidate) return;
    const response = await fetch(API + "/api/runs/" + runId + "/target-selection", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_id: candidate.candidate_id, bundle_name: candidate.bundle_name }),
    });
    if (!response.ok) setError(await response.text()); else setCandidates([]);
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
    else { const latest = await fetch(API + "/api/runs/" + runId); if (latest.ok) setTrace(await latest.json()); }
  };
  const updateProfile = async (profile: ProfileSummary, action: "verify" | "lock" | "rollback") => {
    const response = await fetch(API + "/api/profiles/" + encodeURIComponent(profile.target_app_id ?? profile.profile_id) + "/" + action, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: action === "lock" ? JSON.stringify({ locked: !profile.locked }) : action === "rollback" ? JSON.stringify({ backup_name: profile.history?.at(-1)?.backup_name }) : JSON.stringify({ status: profile.status }),
    });
    if (!response.ok) setError(await response.text());
    else {
      const result = await response.json();
      if (action === "verify" && result.run_id) {
        setRunId(result.run_id as string);
        setTab("live");
        setBusy(true);
      }
      void loadProfiles();
    }
  };
  useEffect(() => { if (tab === "script") void loadScript(); if (tab === "profiles") void loadProfiles(); }, [tab, runId]);

  const latestSnapshot = trace?.snapshots.at(-1);
  const graphArtifactUrl = useCallback((path: string) => artifactUrl(runId, path), [runId]);
  const state = trace?.state ?? (runId ? "created" : "idle");
  const resolved = discovery?.resolved_target ?? trace?.resolved_target;
  const profileStatus = discovery?.profile_status ?? trace?.profile_snapshot?.status ?? trace?.profile_status_at_start ?? "未创建";
  const blockedPaths = discovery?.blocked_paths ?? [];

  return <div className="app-shell">
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
          <div className="section-title"><TerminalSquare size={18} /><span>目标与任务</span></div>
          <div className="target-kind" role="group" aria-label="目标输入类型">
            <button className={targetKind === "app_name" ? "active" : ""} onClick={() => setTargetKind("app_name")}>应用名称</button>
            <button className={targetKind === "bundle_name" ? "active" : ""} onClick={() => setTargetKind("bundle_name")}>bundleName</button>
          </div>
          <label htmlFor="target">{targetKind === "app_name" ? "已安装应用名称" : "精确 bundleName"}</label>
          <div className="input-action"><input id="target" value={targetValue} onChange={(event) => setTargetValue(event.target.value)} placeholder={targetKind === "app_name" ? "例如：备忘录" : "com.example.app"} /><button className="secondary" onClick={resolveTarget} disabled={busy || !targetValue.trim()}><Search size={15} />解析</button></div>
          <label htmlFor="task">自然语言测试目标（可选）</label>
          <textarea id="task" value={task} onChange={(event) => setTask(event.target.value)} rows={4} placeholder="留空时执行启动、探索、返回和重启恢复冒烟测试" />
          <div className="field-row"><div><label htmlFor="mode">运行模式</label><select id="mode" value={mode} onChange={(event) => setMode(event.target.value)}><option value="regression">回归测试</option><option value="exploration">探索测试</option><option value="stability">稳定性测试</option><option value="reproduction">问题复现</option></select></div><div><label>当前 Profile</label><div className="static-field"><StatusBadge value={profileStatus} /></div></div></div>
          <PolicyEditor policy={policy} onChange={setPolicy} />
          <div className="button-row"><button className="primary" onClick={startRun} disabled={busy || !targetValue.trim()}><Play size={17} />启动 Agent</button><button className="danger" onClick={stopRun} disabled={!busy || !runId}><CircleStop size={17} />停止</button></div>
        </section>

        <section className="panel health-panel">
          <div className="section-title"><ServerCog size={18} /><span>运行环境</span><button className="icon-button" onClick={loadHealth} aria-label="刷新环境状态"><RefreshCw size={15} /></button></div>
          <HealthRow icon={<MonitorSmartphone size={16} />} label="HDC Device" value={health?.device.id ?? "unknown"} ok={Boolean(health?.device.connected)} />
          <HealthRow icon={<Bot size={16} />} label="Agent Provider" value={health?.model.provider ?? "unknown"} ok={Boolean(health)} />
          <HealthRow icon={<ShieldCheck size={16} />} label="Hypium" value={health?.hypium.version ?? "not found"} ok={Boolean(health?.hypium.importable)} />
        </section>

        {runId && <section className="panel run-card"><p className="eyebrow">CURRENT RUN</p><code>{runId}</code><div className={"state-pill state-" + state}>{state}</div>{resolved && <p className="resolved-target">{resolved.display_name ?? resolved.app_name ?? resolved.bundle_name}<small>{resolved.bundle_name}</small></p>}<div className="metrics"><Metric label="动作" value={discovery?.actions_executed ?? trace?.actions.length ?? 0} /><Metric label="页面" value={discovery?.pages_discovered ?? trace?.graph.nodes.length ?? 0} /><Metric label="断言" value={trace?.assertions.length ?? 0} /></div></section>}
      </aside>

      <section className="main-panel">
        <nav className="tabs" aria-label="运行结果视图">
          <TabButton active={tab === "live"} onClick={() => setTab("live")} icon={<Activity size={16} />} label="实时执行" />
          <TabButton active={tab === "graph"} onClick={() => setTab("graph")} icon={<GitBranch size={16} />} label="页面关系图" />
          <TabButton active={tab === "script"} onClick={() => setTab("script")} icon={<FileCode2 size={16} />} label="生成脚本" />
          <TabButton active={tab === "profiles"} onClick={() => setTab("profiles")} icon={<ShieldCheck size={16} />} label="Profile" />
          <TabButton active={tab === "report"} onClick={() => setTab("report")} icon={<Braces size={16} />} label="报告" />
        </nav>
        {error && <div className="error-banner" role="alert">{error}</div>}

        {candidates.length > 1 && <section className="selection-banner"><div><strong>发现多个同名应用</strong><span>请选择目标，系统不会自动猜测。</span></div><select value={selectedCandidate} onChange={(event) => setSelectedCandidate(event.target.value)}><option value="">选择应用</option>{candidates.map((candidate) => <option key={candidate.candidate_id ?? candidate.bundle_name} value={candidate.candidate_id ?? candidate.bundle_name}>{candidate.display_name ?? candidate.app_name ?? "未知应用"} · {candidate.bundle_name}</option>)}</select><button className="primary compact" onClick={submitCandidate} disabled={!selectedCandidate}>确认目标</button></section>}

        {tab === "live" && <div className="live-grid">
          <section className="viewport-card"><div className="card-heading"><span>设备画面</span><small>{latestSnapshot ? latestSnapshot.width + " × " + latestSnapshot.height : "waiting"}</small></div><div className="device-stage">{latestSnapshot ? <img src={artifactUrl(runId, latestSnapshot.image_path)} alt={latestSnapshot.summary || "设备截图"} /> : <EmptyState />}</div></section>
          <section className="timeline-card"><div className="card-heading"><span>执行时间线</span><small>{events.length} events</small></div><div className="event-list">{events.length ? events.slice().reverse().map((event) => <div className="event" key={event.event_id}><span className={"event-dot event-" + event.type} /><div><strong>{event.message}</strong><small>{event.type} · {new Date(event.timestamp).toLocaleTimeString()}</small></div></div>) : <EmptyLog />}</div></section>
          <section className="elements-card"><div className="card-heading"><span>当前元素</span><small>{latestSnapshot?.elements.length ?? 0} detected</small></div><div className="element-table">{latestSnapshot?.elements.slice(0, 20).map((element) => <div className="element-row" key={element.element_id}><span className="element-type">{element.type || "Node"}</span><div><strong>{element.content || element.key || element.id}</strong><small>{element.key || element.id || element.source}</small></div><span className={element.clickable || element.editable ? "tag active" : "tag"}>{element.editable ? "input" : element.clickable ? "click" : "read"}</span></div>)}</div></section>
          <DiscoveryPanel discovery={discovery} blockedPaths={blockedPaths} />
        </div>}

        {tab === "graph" && <section className="graph-card">{trace?.graph.nodes.length ? <PageGraphView runId={runId} graph={trace.graph} artifactUrl={graphArtifactUrl} /> : <EmptyState title="页面图尚未生成" />}</section>}
        {tab === "script" && <section className="script-layout"><div className="action-strip"><button className="secondary" onClick={generate} disabled={!runId || busy}><FileCode2 size={16} />重新生成</button><button className="primary" onClick={execute} disabled={!script || busy}><Play size={16} />连续回放 3 次</button></div>{script?.warnings.length ? <div className="warning-list">{script.warnings.map((warning) => <p key={warning}>{warning}</p>)}</div> : null}<pre className="code-view"><code>{script?.python ?? "Profile 通过门禁后将在这里展示确定性生成的 Hypium Python 用例。"}</code></pre></section>}
        {tab === "profiles" && <ProfileManager profiles={profiles} onAction={updateProfile} />}
        {tab === "report" && <section className="report-layout">{runId ? <><div className="report-toolbar"><a className="secondary report-download" href={API + "/api/runs/" + runId + "/report?download=true"}>下载 HTML 报告</a></div><div className="report-card"><iframe title="测试运行报告" src={API + "/api/runs/" + runId + "/report"} /></div></> : <section className="report-card"><EmptyState title="尚无可查看的报告" /></section>}</section>}
      </section>
    </main>
  </div>;
}

function PolicyEditor({ policy, onChange }: { policy: DiscoveryPolicy; onChange: (value: DiscoveryPolicy) => void }) {
  const patch = (value: Partial<DiscoveryPolicy>) => onChange({ ...policy, ...value });
  const permissions: Array<[keyof DiscoveryPolicy, string]> = [["allow_login", "登录"], ["allow_permission", "授权"], ["allow_submit", "提交"], ["allow_publish", "发布"], ["allow_download", "下载"]];
  return <details className="policy-editor"><summary>探索与安全策略</summary><label className="switch-row"><input type="checkbox" checked={policy.enabled} onChange={(event) => patch({ enabled: event.target.checked })} /><span>自动探索（默认开启）</span></label><div className="policy-note">导航、滑动、返回和固定文本输入默认允许；支付、删除、卸载、清除数据始终禁止。</div><div className="permission-grid">{permissions.map(([key, label]) => <label className="check-row" key={key}><input type="checkbox" checked={Boolean(policy[key])} onChange={(event) => patch({ [key]: event.target.checked })} />允许{label}</label>)}</div><div className="limit-grid"><label>页面上限<input type="number" min={1} max={20} value={policy.max_pages} onChange={(event) => patch({ max_pages: Number(event.target.value) })} /></label><label>每页动作<input type="number" min={1} max={8} value={policy.max_actions_per_page} onChange={(event) => patch({ max_actions_per_page: Number(event.target.value) })} /></label><label>超时（秒）<input type="number" min={1} max={900} value={policy.max_duration_seconds} onChange={(event) => patch({ max_duration_seconds: Number(event.target.value) })} /></label></div><label className="switch-row"><input type="checkbox" checked={policy.temporary_test} onChange={(event) => patch({ temporary_test: event.target.checked })} /><span>探索失败后允许受限临时测试</span></label></details>;
}

function DiscoveryPanel({ discovery, blockedPaths }: { discovery: DiscoveryStatus | null; blockedPaths: DiscoveryStatus["blocked_paths"] }) {
  const gates = discovery?.gates ?? {};
  return <section className="discovery-card"><div className="card-heading"><span>探索与晋级门禁</span><small>{discovery?.phase ?? "waiting"}</small></div><div className="gate-grid"><Metric label="定位器" value={discovery?.locator_candidates?.length ?? 0} /><Metric label="断言点" value={discovery?.assertion_candidates?.length ?? 0} /><Metric label="验证轮次" value={discovery?.validation_rounds?.filter((item) => item.passed).length ?? 0} /><Metric label="回放通过" value={discovery?.replays?.filter((item) => item.passed).length ?? 0} /></div>{Object.keys(gates).length > 0 && <div className="gate-list">{Object.entries(gates).map(([name, value]) => <span className={value === true ? "passed" : ""} key={name}>{name}: {String(value)}</span>)}</div>}{blockedPaths?.length ? <div className="blocked-list"><strong>已阻塞路径</strong>{blockedPaths.map((item, index) => <span key={index}>{typeof item === "string" ? item : item.label ?? item.reason ?? item.risk_reason ?? item.target_text ?? "blocked"}</span>)}</div> : null}</section>;
}

function ProfileManager({ profiles, onAction }: { profiles: ProfileSummary[]; onAction: (profile: ProfileSummary, action: "verify" | "lock" | "rollback") => void }) {
  return <section className="profile-layout"><div className="profile-header"><div><p className="eyebrow">PROFILE REGISTRY</p><h2>应用测试资产</h2></div><span>{profiles.length} profiles</span></div><div className="profile-list">{profiles.map((profile) => <article className="profile-card" key={profile.profile_id}><div><strong>{profile.display_name ?? profile.target_app_id ?? profile.profile_id}</strong><code>{profile.bundle_name ?? profile.profile_id}</code></div><StatusBadge value={profile.status} /><dl><div><dt>版本</dt><dd>{profile.version_name ?? profile.version_code ?? "未知"}</dd></div><div><dt>Ability</dt><dd>{profile.main_ability ?? "未知"}</dd></div><div><dt>快速复验</dt><dd>{profile.quick_verification?.passed === undefined ? "未执行" : profile.quick_verification.passed ? "通过" : "失败"}</dd></div></dl><div className="profile-actions"><button className="secondary" onClick={() => onAction(profile, "verify")}><RefreshCw size={14} />{profile.status === "verified" ? "快速复验" : "重新探索验证"}</button><button className="secondary" onClick={() => onAction(profile, "lock")} disabled={profile.status !== "verified"}>{profile.locked ? <Unlock size={14} /> : <Lock size={14} />}{profile.locked ? "解锁" : "锁定"}</button><button className="secondary" onClick={() => onAction(profile, "rollback")} disabled={profile.status !== "verified" || profile.locked || !profile.history?.length}><RefreshCw size={14} />回退</button></div></article>)}{profiles.length === 0 && <EmptyState title="暂无 Profile" />}</div></section>;
}

function StatusBadge({ value }: { value: string }) { return <span className={"profile-status status-" + value}>{value}</span>; }
function StatusDot({ ok, label }: { ok: boolean; label: string }) { return <span className="status-dot"><i className={ok ? "ok" : "muted"} />{label}</span>; }
function HealthRow({ icon, label, value, ok }: { icon: ReactNode; label: string; value: string; ok: boolean }) { return <div className="health-row"><span className="health-icon">{icon}</span><div><small>{label}</small><strong>{value}</strong></div><i className={ok ? "health-ok" : "health-off"} /></div>; }
function Metric({ label, value }: { label: string; value: number }) { return <div><strong>{String(value).padStart(2, "0")}</strong><small>{label}</small></div>; }
function TabButton({ active, onClick, icon, label }: { active: boolean; onClick: () => void; icon: ReactNode; label: string }) { return <button className={active ? "tab active" : "tab"} onClick={onClick}>{icon}{label}</button>; }
function EmptyState({ title = "等待 Agent 采集设备画面" }: { title?: string }) { return <div className="empty-state"><MonitorSmartphone size={38} /><strong>{title}</strong><span>运行状态和证据会实时出现在这里</span></div>; }
function EmptyLog() { return <div className="empty-log"><Activity size={24} /><span>启动任务后显示目标解析、探索、工具调用和断言事件</span></div>; }
function artifactUrl(runId: string, absolutePath: string) { const normalized = absolutePath.replaceAll("\\", "/"); const marker = "/" + runId + "/"; const index = normalized.indexOf(marker); const relative = index >= 0 ? normalized.slice(index + marker.length) : normalized.split("/").slice(-2).join("/"); return API + "/api/runs/" + runId + "/artifacts/" + relative; }
