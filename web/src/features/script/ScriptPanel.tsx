// Hypium 脚本库：统一列出 Live 运行与直流会话生成的脚本，选中即展示源码，
// 并可按来源启动：Live 走验收回放，直流脚本走设备诊断执行。
// 「立即可用」：按钮默认可用，质量顾虑只作提示（confidence_factors），
// 只有物理上不可执行（runnable_blockers 非空）时才用 role="alert" 说明原因。

import { useCallback, useEffect, useMemo, useState } from "react";
import { ExternalLink, FileCode2, Play, RefreshCw } from "lucide-react";
import clsx from "clsx";
import { Badge, EmptyState } from "../../components/ui/primitives";
import { apiError } from "../../api/client";
import { dcArtifactUrl } from "../../api/dc-client";
import { getRun, getScript, listScripts, runDcScript, startRunReplay } from "../../api/scripts";
import type { ScriptCatalogEntry, ScriptDetail } from "../../api/scripts";
import type { ReplayResult, RunTrace } from "../../api/types";
import { useConsole } from "../../stores/console";
import { artifactUrl } from "../../utils/artifact";
import { evidenceLabel, confidenceLabel, confidenceTone, formatDuration, replayStatus, statusLabel } from "../../utils/format";

/** 验收回放结果（含轮询到的 trace） */
type ReplayOutcome = { kind: "run"; runId: string; attempts: number; replays: ReplayResult[] };
/** 直流脚本诊断执行结果 */
type DiagnosticOutcome = { kind: "dc"; sessionId: string; results: ReplayResult[] };
type Outcome = ReplayOutcome | DiagnosticOutcome;

/** 目录项展示身份：DC 未入库脚本不能用悬空的 case_id 冒充用例身份。 */
function displayName(entry: ScriptCatalogEntry): string {
  if (entry.source === "dc" && entry.case_persisted !== true) return entry.filename;
  return entry.case_id ?? entry.filename;
}

/** DC 录制尚未入库时的提示（对应 POST /api/cases/from-dc/{session_id}）。 */
const CASE_PERSIST_HINT = "本次录制尚未保存为可复用用例，可用 POST /api/cases/from-dc/{session_id} 入库。";

export function ScriptPanel() {
  const runId = useConsole((state) => state.runId);
  const generate = useConsole((state) => state.generate);
  const operation = useConsole((state) => state.operation);
  const attemptCount = useConsole((state) => state.attemptCount);
  const patchForm = useConsole((state) => state.patchForm);

  const [entries, setEntries] = useState<ScriptCatalogEntry[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<ScriptDetail | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [launching, setLaunching] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const refreshList = useCallback(async () => {
    try {
      const next = await listScripts();
      setEntries(next);
      setError("");
      return next;
    } catch (cause) {
      setError(apiError("读取脚本列表失败", cause));
      return [];
    }
  }, []);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  // 选中项：保留仍然存在的选择，否则优先当前运行的脚本，最后取最新一条
  useEffect(() => {
    if (entries.length === 0) {
      if (selectedId) setSelectedId("");
      return;
    }
    if (selectedId && entries.some((entry) => entry.script_id === selectedId)) return;
    const currentRun = runId ? entries.find((entry) => entry.run_id === runId) : undefined;
    setSelectedId((currentRun ?? entries[0]).script_id);
  }, [entries, runId, selectedId]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    void (async () => {
      try {
        const next = await getScript(selectedId);
        if (!cancelled) {
          setDetail(next);
          setError("");
        }
      } catch (cause) {
        if (!cancelled) {
          setDetail(null);
          setError(apiError("读取脚本内容失败", cause));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const entry = detail?.entry;
  // 「立即可用」：Live 脚本只要物理上可执行（replay_eligible）按钮就可用，质量顾虑只做提示；
  // 直流脚本走「诊断启动」这条显式不计入验收结论的执行路径，因此始终可启动。
  const launchable = Boolean(entry) && (entry!.source === "dc" || entry!.replay_eligible);
  const qualityNotes = useMemo(
    () => entry?.confidence_factors ?? entry?.incomplete_reasons ?? [],
    [entry],
  );
  const runnableBlockers = entry?.runnable_blockers ?? [];

  const handleCopy = () => {
    if (detail?.python) void navigator.clipboard.writeText(detail.python);
  };

  const handleDownload = () => {
    if (!detail?.python || !entry) return;
    const blob = new Blob([detail.python], { type: "text/x-python" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = entry.filename || "test.py";
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleLaunch = async () => {
    if (!entry) return;
    setLaunching(true);
    setError("");
    setOutcome(null);
    try {
      if (entry.source === "dc") {
        const response = await runDcScript(entry.script_id, 1);
        setOutcome({ kind: "dc", sessionId: response.session_id, results: response.results });
      } else {
        await startRunReplay(entry.run_id, attemptCount);
        const trace = await pollReplay(entry.run_id);
        setOutcome({ kind: "run", runId: entry.run_id, attempts: attemptCount, replays: trace?.replays ?? [] });
      }
    } catch (cause) {
      setError(apiError("启动脚本失败", cause));
    } finally {
      setLaunching(false);
    }
  };

  const handleGenerate = async () => {
    await generate();
    await refreshList();
  };

  const busy = launching || operation !== "idle";

  return (
    <section className="panel script-library">
      <div className="script-library-head">
        <div className="card-heading">
          <span>Hypium Python 脚本</span>
          <small>{entries.length} 个脚本 · 共 {entries.filter((item) => item.source === "dc").length} 个直流录制</small>
        </div>
        <div className="script-library-actions">
          <button
            type="button"
            className="secondary compact"
            onClick={() => void refreshList()}
            disabled={loading}
          >
            <RefreshCw size={14} />刷新
          </button>
          <button type="button" className="secondary compact" onClick={() => void handleGenerate()} disabled={!runId || busy}>
            <FileCode2 size={14} />重新生成当前运行脚本
          </button>
        </div>
      </div>

      {error && <div className="banner error-banner" role="alert">{error}</div>}

      <div className="script-library-body">
        <div className="script-list" role="listbox" aria-label="脚本列表">
          {entries.length === 0 && (
            <p className="script-list-empty">尚无生成的脚本。运行 Agent 或使用直流模式录制后，脚本会出现在这里。</p>
          )}
          {entries.map((item) => (
            <button
              type="button"
              key={item.script_id}
              role="option"
              aria-selected={item.script_id === selectedId}
              className={clsx("script-item", item.script_id === selectedId && "active")}
              onClick={() => setSelectedId(item.script_id)}
            >
              <span className="script-item-top">
                <strong title={displayName(item)}>{displayName(item)}</strong>
                <Badge tone={item.source === "dc" ? "brand" : "neutral"}>{item.source === "dc" ? "直流" : "Live"}</Badge>
                <Badge tone={confidenceTone(item.confidence)}>{confidenceLabel(item.confidence)}</Badge>
                {!item.replay_eligible && <Badge tone="danger">不可执行</Badge>}
              </span>
              <small>
                {item.run_id}
                {item.run_id === runId ? " · 当前运行" : ""}
              </small>
              <small>
                {item.included_actions != null ? `${item.included_actions} 步` : "步数未知"}
                {item.omitted_actions ? ` · 省略 ${item.omitted_actions}` : ""}
                {item.modified_at ? ` · ${formatStamp(item.modified_at)}` : ""}
              </small>
            </button>
          ))}
        </div>

        <div className="script-detail">
          {!entry && (
            <EmptyState
              title="选择一个脚本"
              hint="左侧列表包含 Live 运行与直流模式生成的 Hypium 脚本"
              icon={<FileCode2 size={34} />}
            />
          )}
          {entry && (
            <>
              <div className="script-detail-head">
                <div>
                  <strong>{displayName(entry)}</strong>
                  <small>{entry.script_id}</small>
                  {entry.case_persisted === true && (
                    <small>用例 ID：<code>{entry.case_id}</code></small>
                  )}
                </div>
                <div className="script-detail-badges">
                  <Badge tone={entry.source === "dc" ? "brand" : "neutral"}>
                    {entry.source === "dc" ? "直流录制" : "Live 运行"}
                  </Badge>
                  <Badge tone={entry.replay_eligible ? "ok" : "danger"}>
                    {entry.replay_eligible ? `可执行 · ${confidenceLabel(entry.confidence)}` : "不可执行"}
                  </Badge>
                </div>
              </div>

              <div className="script-toolbar">
                <span className="script-stat">
                  可回放操作：{entry.included_actions ?? "—"}
                  {entry.omitted_actions ? ` · 省略：${entry.omitted_actions}` : ""}
                </span>
                {entry.source === "run" && (
                  <label className="attempt-select" htmlFor="attempts">回放次数
                    <select
                      id="attempts"
                      value={attemptCount}
                      onChange={(event) => patchForm({ attemptCount: Number(event.target.value) as 1 | 3 })}
                      disabled={busy}
                    >
                      <option value={1}>1 次</option>
                      <option value={3}>3 次</option>
                    </select>
                  </label>
                )}
                <span className="spacer" />
                <button type="button" className="secondary compact" onClick={handleCopy}>复制</button>
                <button type="button" className="secondary compact" onClick={handleDownload}>下载</button>
                <button
                  type="button"
                  className="primary compact"
                  onClick={() => void handleLaunch()}
                  disabled={!launchable || busy}
                  title={
                    launchable
                      ? entry.source === "dc"
                        ? "在设备上诊断执行该直流录制脚本（不计入验收结论）"
                        : `对该运行执行验收回放 ${attemptCount} 次`
                      : "脚本缺少可回放动作或使用了占位应用身份"
                  }
                >
                  <Play size={14} />
                  {launching
                    ? "启动中..."
                    : entry.source === "dc"
                      ? "诊断启动"
                      : `验收回放 ${attemptCount} 次`}
                </button>
              </div>

              {qualityNotes.length > 0 && (
                <div className="warning-list" role="note">
                  <p>质量提示（不影响执行）：置信度 {confidenceLabel(entry.confidence)}</p>
                  {qualityNotes.map((reason) => <p key={reason}>{reason}</p>)}
                </div>
              )}
              {entry.source === "dc" && entry.case_persisted === false && (
                <div className="warning-list" role="note">
                  <p>{CASE_PERSIST_HINT}</p>
                </div>
              )}
              {!entry.replay_eligible && (
                <div className="warning-list" role="alert">
                  <p>该脚本当前不可执行：</p>
                  {runnableBlockers.map((blocker) => <p key={blocker}>{blocker}</p>)}
                </div>
              )}
              {entry.warnings.length > 0 && (
                <div className="warning-list" role="alert">
                  {entry.warnings.map((warning) => <p key={warning}>{warning}</p>)}
                </div>
              )}

              {outcome && <OutcomeView outcome={outcome} />}

              <pre className="code-view">
                <code>{loading && !detail.python ? "读取中..." : detail.python}</code>
              </pre>
            </>
          )}
        </div>
      </div>
    </section>
  );
}

/** 启动结果：Live 走验收回放语义，直流走诊断执行语义，两者证据链接前缀不同。 */
function OutcomeView({ outcome }: { outcome: Outcome }) {
  const results = outcome.kind === "run" ? outcome.replays : outcome.results;
  const running = outcome.kind === "run" && results.length < outcome.attempts;
  return (
    <section className="replay-progress" aria-live="polite" aria-busy={running}>
      <div className="progress-heading">
        {outcome.kind === "run" ? "验收回放结果" : "诊断执行结果"}
        <span>
          {running ? "执行中" : `${results.filter((item) => item.passed).length}/${results.length || 1} 通过`}
        </span>
      </div>
      <div className="attempt-grid">
        {results.map((attempt) => (
          <article className="attempt-card" key={attempt.attempt}>
            <div className="attempt-top">
              Attempt {attempt.attempt}
              <Badge
                tone={
                  attempt.passed
                    ? "ok"
                    : ["failed", "timed_out", "invalid_result"].includes(replayStatus(attempt))
                      ? "danger"
                      : "warn"
                }
              >
                {statusLabel(replayStatus(attempt))}
              </Badge>
            </div>
            <dl>
              <div><dt>耗时</dt><dd>{formatDuration(attempt.command.duration_ms)}</dd></div>
              <div><dt>退出码</dt><dd>{attempt.exit_code ?? attempt.command.returncode ?? "—"}</dd></div>
            </dl>
            {attempt.error?.message && <p className="attempt-error" role="alert">{attempt.error.message}</p>}
            {attempt.evidence_paths.length > 0 && (
              <div className="evidence-links">
                {attempt.evidence_paths.map((path) => (
                  <a
                    key={path}
                    href={
                      outcome.kind === "dc"
                        ? dcArtifactUrl(outcome.sessionId, path)
                        : artifactUrl(outcome.runId, path)
                    }
                    target="_blank"
                    rel="noreferrer"
                  >
                    <ExternalLink size={11} />{evidenceLabel(path)}
                  </a>
                ))}
              </div>
            )}
          </article>
        ))}
        {!running && results.length === 0 && <p className="script-list-empty">未产生回放结果。</p>}
      </div>
    </section>
  );
}

/** 轮询运行轨迹直到验收回放离开 pending（上限约 7.5 分钟防呆）。 */
async function pollReplay(runId: string): Promise<RunTrace | null> {
  let trace = await getRun(runId);
  let ticks = 0;
  while (trace?.replay_status === "pending" && ticks < 500) {
    await new Promise((resolve) => window.setTimeout(resolve, 900));
    trace = await getRun(runId);
    ticks += 1;
  }
  return trace;
}

function formatStamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
