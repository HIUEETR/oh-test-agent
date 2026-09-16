// DC 会话「蒸馏为 Profile」按钮：把当前会话沉淀为 Profile 资产（2026-09-17 重构亮点）。
//
// 交互约束：
// - 必须显式填写真实 bundleName / Ability：占位值（com.example.app / EntryAbility）
//   会被后端拒绝为 422，避免把示例身份蒸馏成 Profile；
// - 蒸馏同步等待 1 轮设备验证 + 1 次 Hypium 回放（<2 分钟），期间禁用按钮并给出进度文案；
// - 结果通过 SSE（profile_distill_started/finished/failed）与会话日志同时可见。

import { useState } from "react";
import { FlaskConical } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";

const PLACEHOLDER_BUNDLES = new Set(["", "com.example.app"]);
const PLACEHOLDER_ABILITIES = new Set(["", "EntryAbility"]);

export function DcDistillButton() {
  const activeSessionId = useDcConsole((state) => state.activeSessionId);
  const invocationCount = useDcConsole((state) => state.session?.invocations.length ?? 0);
  const distillProfile = useDcConsole((state) => state.distillProfile);
  const distillResult = useDcConsole((state) => state.distillResult);

  const [bundleName, setBundleName] = useState("");
  const [mainAbility, setMainAbility] = useState("");
  const [distilling, setDistilling] = useState(false);
  const [localError, setLocalError] = useState("");

  // 按钮保持可点击（有会话与录制即可）：占位身份在点击时给出明确原因，
  // 而不是用一个「看起来坏了」的禁用按钮让用户猜为什么不能点。
  const disabled = distilling || !activeSessionId || invocationCount === 0;

  const handleDistill = async () => {
    setLocalError("");
    if (PLACEHOLDER_BUNDLES.has(bundleName.trim())) {
      setLocalError("请填写真实 bundleName（不能是 com.example.app）");
      return;
    }
    if (PLACEHOLDER_ABILITIES.has(mainAbility.trim())) {
      setLocalError("请填写真实 Ability 名称（不能是 EntryAbility）");
      return;
    }
    setDistilling(true);
    try {
      await distillProfile(bundleName.trim(), mainAbility.trim());
    } finally {
      setDistilling(false);
    }
  };

  return (
    <section className="panel dc-distill">
      <div className="card-heading">
        <span>蒸馏为 Profile</span>
        <small>{distillResult ? `${distillResult.status}` : "会话沉淀"}</small>
      </div>
      <div className="dc-distill-fields">
        <label htmlFor="dc-distill-bundle">bundleName</label>
        <input
          id="dc-distill-bundle"
          value={bundleName}
          placeholder="com.example.notes"
          onChange={(event) => setBundleName(event.target.value)}
          disabled={distilling}
        />
        <label htmlFor="dc-distill-ability">Main Ability</label>
        <input
          id="dc-distill-ability"
          value={mainAbility}
          placeholder="MainAbility"
          onChange={(event) => setMainAbility(event.target.value)}
          disabled={distilling}
        />
      </div>
      <div className="dc-distill-actions">
        <button
          type="button"
          className="primary compact"
          onClick={() => void handleDistill()}
          disabled={disabled}
        >
          <FlaskConical size={14} />
          {distilling ? "蒸馏中…" : "蒸馏为 Profile"}
        </button>
      </div>
      {invocationCount === 0 && <small className="dc-distill-hint">先录制至少 3 个页面的操作再蒸馏。</small>}
      {localError && <small className="dc-distill-error">{localError}</small>}
      {distillResult && (
        <div className="dc-distill-result">
          <small>
            状态：{distillResult.status} · 页面 {distillResult.pages_covered} · 定位器{" "}
            {distillResult.stable_locators} · 断言 {distillResult.assertions}
          </small>
          {distillResult.warnings.slice(0, 3).map((warning, index) => (
            <small key={index}>⚠ {warning}</small>
          ))}
        </div>
      )}
    </section>
  );
}
