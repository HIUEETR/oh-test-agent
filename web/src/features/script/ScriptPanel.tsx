// Hypium 脚本面板：脚本生成/重新生成、验收回放控制与回放进度。

import { useEffect } from "react";
import { ExternalLink, FileCode2, Play, RefreshCw } from "lucide-react";
import { Badge } from "../../components/ui/primitives";
import { useConsole } from "../../stores/console";
import { artifactUrl } from "../../utils/artifact";
import { evidenceLabel, formatDuration, replayStatus, statusLabel } from "../../utils/format";
import type { ReplayResult } from "../../api/types";

export function ScriptPanel() {
  const runId = useConsole((state) => state.runId);
  const trace = useConsole((state) => state.trace);
  const script = useConsole((state) => state.script);
  const operation = useConsole((state) => state.operation);
  const attemptCount = useConsole((state) => state.attemptCount);
  const patchForm = useConsole((state) => state.patchForm);
  const loadScript = useConsole((state) => state.loadScript);
  const generate = useConsole((state) => state.generate);
  const execute = useConsole((state) => state.execute);

  // 每次进入脚本页或运行切换时静默刷新脚本内容。
  useEffect(() => { void loadScript(true); }, [loadScript, runId]);

  const scriptDiagnostic = script?.diagnostic
    ?? (script?.purpose ? script.purpose === "diagnostic" : !["regression", "stability"].includes(trace?.mode ?? "regression"));
  const replayAllowed = (script?.acceptance_replay_enabled ?? !scriptDiagnostic) && !trace?.provisional && !trace?.live_mode;
  const scriptStatus = operation === "generating" ? "生成中"
    : script ? (scriptDiagnostic ? "诊断脚本" : "已生成")
    : trace?.generated ? "正在读取" : "待自动生成";
  const operationBusy = operation !== "idle";
  const completedAttempts = trace?.replay_completed
    ?? trace?.replays.filter((item) => ["passed", "failed", "timed_out", "ineligible", "invalid_result"].includes(replayStatus(item))).length
    ?? 0;
  const progress = operation === "executing"
    ? Math.min(95, Math.max(8, (completedAttempts / attemptCount) * 100))
    : trace?.replays.length ? 100 : 0;

  return (
    <section className="panel">
      <div className="card-heading">
        <span>Hypium Python 脚本</span>
        <Badge tone={scriptDiagnostic ? "warn" : "ok"}>{scriptStatus}</Badge>
      </div>
      <div className="script-toolbar">
        <button type="button" className="secondary" onClick={generate} disabled={!runId || operationBusy}>
          <FileCode2 size={15} />{script ? "重新生成" : "生成脚本"}
        </button>
        <label className="attempt-select" htmlFor="attempts">回放次数
          <select
            id="attempts"
            value={attemptCount}
            onChange={(event) => patchForm({ attemptCount: Number(event.target.value) as 1 | 3 })}
            disabled={operationBusy}
          >
            <option value={1}>1 次</option>
            <option value={3}>3 次</option>
          </select>
        </label>
        <button
          type="button"
          className="primary compact"
          onClick={execute}
          disabled={!script || operationBusy || !replayAllowed}
          title={!replayAllowed ? "诊断或临时 Profile 脚本不能用于验收回放" : undefined}
        >
          <Play size={15} />验收回放 {attemptCount} 次
        </button>
        <span className="spacer" />
        <span className="badge">Agent 运行成功后自动生成，也可手动重新生成</span>
      </div>

      {!replayAllowed && (
        <div className="warning-list" role="alert">当前脚本或 Profile 不具备正式 Hypium 验收回放资格。</div>
      )}
      {script?.warnings.length ? (
        <div className="warning-list" role="alert">{script.warnings.map((warning) => <p key={warning}>{warning}</p>)}</div>
      ) : null}

      {(operation === "executing" || Boolean(trace?.replays.length)) && (
        <ReplayProgress
          replays={trace?.replays ?? []}
          attempts={attemptCount}
          running={operation === "executing"}
          progress={progress}
          runId={runId}
        />
      )}

      <pre className="code-view"><code>{script?.python ?? "运行 Agent 后将在这里展示自动生成的 Hypium Python 用例。"}</code></pre>
    </section>
  );
}

function ReplayProgress({ replays, attempts, running, progress, runId }: {
  replays: ReplayResult[]; attempts: 1 | 3; running: boolean; progress: number; runId: string;
}) {
  return (
    <section className="replay-progress" aria-live="polite" aria-busy={running}>
      <div className="progress-heading">
        异步回放进度
        <span>{running ? "执行中" : `${replays.filter((item) => item.passed).length}/${replays.length || attempts} 通过`}</span>
      </div>
      <div className="progress-track"><i style={{ width: `${progress}%` }} /></div>
      <div className="attempt-grid">
        {Array.from({ length: attempts }, (_, index) => {
          const attempt = replays.find((item) => item.attempt === index + 1);
          const status = attempt ? replayStatus(attempt) : running && index === replays.length ? "running" : "queued";
          const duration = attempt?.command.duration_ms;
          const exitCode = attempt?.exit_code ?? attempt?.command.returncode;
          const failure = attempt?.error?.message || attempt?.command.stderr || (attempt?.timed_out ? "执行超时" : "");
          return (
            <article className="attempt-card" key={index}>
              <div className="attempt-top">
                Attempt {index + 1}
                <Badge tone={status === "passed" ? "ok" : ["failed", "timed_out", "invalid_result"].includes(status) ? "danger" : "warn"}>
                  {statusLabel(status)}
                </Badge>
              </div>
              <dl>
                <div><dt>耗时</dt><dd>{duration === undefined ? "—" : formatDuration(duration)}</dd></div>
                <div><dt>退出码</dt><dd>{exitCode ?? "—"}</dd></div>
              </dl>
              {failure && <p className="attempt-error" role="alert">{failure}</p>}
              {attempt && attempt.evidence_paths.length > 0 && (
                <div className="evidence-links">
                  {attempt.evidence_paths.map((path, evidenceIndex) => (
                    <a href={artifactUrl(runId, path)} target="_blank" rel="noreferrer" key={`${path}-${evidenceIndex}`}>
                      <ExternalLink size={11} />{evidenceLabel(path)}
                    </a>
                  ))}
                </div>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
