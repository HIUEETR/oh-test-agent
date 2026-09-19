// DC 会话输入区控制条右侧的两个动作 pill（DeepSeek 式 composer）：
// - Hypium 脚本：一键生成（已生成则直接预览），生成完成自动弹出预览；
// - 蒸馏为 Profile：身份由后端从会话录制推断，一键触发；推断失败（422 cannot infer）
//   时给出「手动填写身份」兜底浮层，用显式身份重试一次。
//
// 「任务完成」统一定义为 taskDone：有会话 + 会话未关闭 + 有录制记录 + 非执行中。
// 执行中/无录制一律 disabled，不依赖后端兜底。
// 浮层用 createPortal 挂到 body：.panel 的 backdrop-filter 会困住面板内 fixed 元素。

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { FileCode2, FlaskConical } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";
import { DcScriptPreview } from "./DcScriptPreview";

export function DcComposerActions() {
  const activeSessionId = useDcConsole((state) => state.activeSessionId);
  const session = useDcConsole((state) => state.session);
  const status = useDcConsole((state) => state.status);
  const cancelStatus = useDcConsole((state) => state.cancelStatus);
  const invocationCount = useDcConsole((state) => state.session?.invocations.length ?? 0);
  const script = useDcConsole((state) => state.script);
  const distillResult = useDcConsole((state) => state.distillResult);
  const generateScript = useDcConsole((state) => state.generateScript);
  const distillProfile = useDcConsole((state) => state.distillProfile);

  const [generating, setGenerating] = useState(false);
  const [distilling, setDistilling] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [identityOpen, setIdentityOpen] = useState(false);
  const [hint, setHint] = useState<{ tone: "info" | "error"; text: string; retry: boolean } | null>(null);

  const busy = status === "thinking" || status === "acting" || cancelStatus === "confirming";
  const taskDone =
    Boolean(activeSessionId) && session?.status !== "closed" && invocationCount > 0 && !busy;
  const disabled = !taskDone;
  const disabledTitle = "任务完成且有录制记录后可点击";

  const handleScript = async () => {
    if (disabled || generating || distilling) return;
    setHint(null);
    // 已有脚本：直接预览，不重复请求
    if (useDcConsole.getState().script) {
      setPreviewOpen(true);
      return;
    }
    setGenerating(true);
    try {
      await generateScript();
      if (useDcConsole.getState().script) setPreviewOpen(true);
      else {
        const detail = useDcConsole.getState().error;
        if (detail) setHint({ tone: "error", text: detail, retry: false });
      }
    } finally {
      setGenerating(false);
    }
  };

  const handleDistill = async (bundleName?: string, mainAbility?: string) => {
    if (disabled || distilling || generating) return;
    setHint(null);
    setDistilling(true);
    try {
      const result = await distillProfile(bundleName, mainAbility);
      if (result) {
        setIdentityOpen(false);
        return;
      }
      const detail = useDcConsole.getState().error;
      setHint({
        tone: "error",
        text: detail || "蒸馏 Profile 失败",
        retry: detail.includes("cannot infer"),
      });
    } finally {
      setDistilling(false);
    }
  };

  const closePreview = useCallback(() => setPreviewOpen(false), []);
  const closeIdentity = useCallback(() => setIdentityOpen(false), []);

  return (
    <>
      <div className="dc-composer-pills">
        <button
          type="button"
          className={`dc-pill${generating ? " busy" : ""}`}
          onClick={() => void handleScript()}
          disabled={disabled || generating || distilling}
          title={disabled ? disabledTitle : script ? "查看已生成的脚本" : "从本会话录制生成 Hypium 脚本"}
          aria-label="Hypium 脚本"
        >
          {generating ? <span className="dc-tool-spinner" aria-hidden="true" /> : <FileCode2 size={13} aria-hidden="true" />}
          <span>{generating ? "生成中…" : "Hypium 脚本"}</span>
        </button>

        <button
          type="button"
          className={`dc-pill${distilling ? " busy" : ""}`}
          onClick={() => void handleDistill()}
          disabled={disabled || generating || distilling}
          title={disabled ? disabledTitle : "从本会话录制蒸馏 Profile 资产（后端自动推断应用身份）"}
          aria-label="蒸馏为 Profile"
        >
          {distilling ? <span className="dc-tool-spinner" aria-hidden="true" /> : <FlaskConical size={13} aria-hidden="true" />}
          <span>{distilling ? "蒸馏中…（约 2 分钟）" : "蒸馏为 Profile"}</span>
        </button>
      </div>

      {/* hint 行：蒸馏摘要 / 错误文案 + 手动身份兜底入口 */}
      {distillResult && !hint && (
        <div className="dc-composer-hint">
          状态：{distillResult.status} · 页面 {distillResult.pages_covered} · 定位器 {distillResult.stable_locators} · 断言{" "}
          {distillResult.assertions} · 可在「Profile 资产」Tab 查看
        </div>
      )}
      {hint && (
        <div className={`dc-composer-hint${hint.tone === "error" ? " error" : ""}`} role="status">
          {hint.text}
          {hint.retry && (
            <button
              type="button"
              className="dc-hint-link"
              onClick={() => setIdentityOpen(true)}
            >
              手动填写身份
            </button>
          )}
        </div>
      )}

      {identityOpen &&
        createPortal(
          <div className="dc-identity-overlay" onClick={closeIdentity} role="presentation">
            <div
              className="dc-identity-pop"
              role="dialog"
              aria-modal="true"
              aria-label="手动填写应用身份"
              onClick={(event) => event.stopPropagation()}
            >
              <strong>手动填写应用身份</strong>
              <small>后端无法从会话录制推断 bundleName / MainAbility，请显式指定后重试。</small>
              <IdentityForm
                distilling={distilling}
                onCancel={closeIdentity}
                onSubmit={(bundleName, mainAbility) => void handleDistill(bundleName, mainAbility)}
              />
            </div>
          </div>,
          document.body,
        )}

      <DcScriptPreview open={previewOpen} onClose={closePreview} />
    </>
  );
}

/** 手动身份表单：非空校验在本地完成，避免把空值当成「显式身份」发给后端。 */
function IdentityForm({
  distilling,
  onCancel,
  onSubmit,
}: {
  distilling: boolean;
  onCancel: () => void;
  onSubmit: (bundleName: string, mainAbility: string) => void;
}) {
  const [bundleName, setBundleName] = useState("");
  const [mainAbility, setMainAbility] = useState("");
  const [error, setError] = useState("");

  // 浮层打开期间 Esc 关闭
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);

  const submit = () => {
    if (!bundleName.trim() || !mainAbility.trim()) {
      setError("bundleName 与 MainAbility 都不能为空");
      return;
    }
    setError("");
    onSubmit(bundleName.trim(), mainAbility.trim());
  };

  return (
    <>
      <label htmlFor="dc-identity-bundle">bundleName</label>
      <input
        id="dc-identity-bundle"
        aria-label="bundleName"
        value={bundleName}
        placeholder="com.example.notes"
        onChange={(event) => setBundleName(event.target.value)}
        disabled={distilling}
      />
      <label htmlFor="dc-identity-ability">MainAbility</label>
      <input
        id="dc-identity-ability"
        aria-label="MainAbility"
        value={mainAbility}
        placeholder="MainAbility"
        onChange={(event) => setMainAbility(event.target.value)}
        disabled={distilling}
      />
      {error && <small className="dc-identity-error">{error}</small>}
      <div className="dc-identity-actions">
        <button type="button" className="secondary compact" onClick={onCancel}>取消</button>
        <button type="button" className="primary compact" onClick={submit} disabled={distilling}>
          {distilling ? "蒸馏中…" : "用该身份重试"}
        </button>
      </div>
    </>
  );
}
