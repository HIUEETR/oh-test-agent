// Profile 资产面板：快速复验、锁定、回退、失效与「3 次连续成功」回放进度。
// （补齐后端已有但旧 UI 缺失的 invalidate 入口，2026-09-17 增加异步追加回放入口。）

import { useEffect, useState } from "react";
import { Ban, ListChecks, Lock, RefreshCw, RotateCcw, ShieldCheck, Unlock } from "lucide-react";
import { Badge, EmptyState } from "../../components/ui/primitives";
import { replayProfile } from "../../api/dc-client";
import { useConsole, type ProfileAction } from "../../stores/console";
import type { ProfileSummary } from "../../api/types";

/** 比赛「3 次连续成功」要求的默认回放证据总量。 */
const DEFAULT_MAX_REPLAYS = 3;

/** 状态徽章色调。 */
function statusTone(status: string): "ok" | "warn" | "danger" | "brand" | "neutral" {
  if (status === "verified") return "ok";
  if (status === "invalid") return "danger";
  if (status === "candidate") return "brand";
  if (status === "draft") return "warn";
  return "neutral";
}

export function ProfilesPanel() {
  const profiles = useConsole((state) => state.profiles);
  const loadProfiles = useConsole((state) => state.loadProfiles);
  const updateProfile = useConsole((state) => state.updateProfile);

  useEffect(() => { void loadProfiles(true); }, [loadProfiles]);

  return (
    <section className="panel">
      <div className="card-heading">
        <span>应用测试资产（Profile）</span>
        <small>{profiles.length} 个</small>
      </div>
      <p className="note">verified Profile 可让后续运行跳过完整探索；失效后需要重新探索并晋级。</p>
      <div className="profile-list">
        {profiles.map((profile) => (
          <ProfileCard
            key={profile.profile_id}
            profile={profile}
            onAction={updateProfile}
            onReload={() => void loadProfiles(true)}
          />
        ))}
        {profiles.length === 0 && (
          <EmptyState title="暂无 Profile" hint="探索运行完成后会自动沉淀 Profile 资产" icon={<ShieldCheck size={34} />} />
        )}
      </div>
    </section>
  );
}

function ProfileCard({
  profile,
  onAction,
  onReload,
}: {
  profile: ProfileSummary;
  onAction: (profile: ProfileSummary, action: ProfileAction) => void;
  onReload: () => void;
}) {
  const [replaying, setReplaying] = useState(false);
  const [replayError, setReplayError] = useState("");
  const maxReplays = profile.max_replays ?? DEFAULT_MAX_REPLAYS;
  const recorded = profile.hypium_replay_run_ids?.length ?? 0;
  const canReplay = Boolean(profile.generated_script_path) && profile.status !== "invalid" && !profile.locked;
  const remaining = Math.max(maxReplays - recorded, 0);

  const handleReplay = async () => {
    setReplayError("");
    setReplaying(true);
    try {
      await replayProfile(profile.target_app_id ?? profile.profile_id, remaining || 1);
      onReload();
    } catch (cause) {
      setReplayError(cause instanceof Error ? cause.message : "追加回放失败");
    } finally {
      setReplaying(false);
    }
  };

  return (
    <article className="profile-card">
      <div className="profile-head">
        <div style={{ minWidth: 0 }}>
          <strong>{profile.display_name ?? profile.target_app_id ?? profile.profile_id}</strong>
          <code>{profile.bundle_name ?? profile.profile_id}</code>
        </div>
        <Badge tone={statusTone(profile.status)}>{profile.status}</Badge>
      </div>
      <div className="profile-actions">
        <button type="button" className="secondary" onClick={() => onAction(profile, "verify")}>
          <RefreshCw size={13} />快速复验
        </button>
        <button type="button" className="secondary" onClick={() => onAction(profile, "lock")} disabled={profile.status !== "verified"}>
          {profile.locked ? <Unlock size={13} /> : <Lock size={13} />}{profile.locked ? "解锁" : "锁定"}
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => onAction(profile, "rollback")}
          disabled={profile.status !== "verified" || profile.locked || !profile.history?.length}
        >
          <RotateCcw size={13} />回退
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => void handleReplay()}
          disabled={!canReplay || replaying || remaining === 0}
          title={canReplay ? "在设备空闲时异步追加 Hypium 回放证据" : "该 Profile 没有可回放的生成脚本"}
        >
          <ListChecks size={13} />
          {replaying ? "回放中…" : `追加 ${remaining || 0} 次回放`}
        </button>
        <button type="button" className="danger compact" onClick={() => onAction(profile, "invalidate")} disabled={profile.status !== "verified"}>
          <Ban size={13} />失效
        </button>
      </div>

      <div className="profile-replay-progress">
        <progress value={recorded} max={maxReplays} aria-label="Hypium 回放证据进度" />
        <small>{recorded}/{maxReplays} 次回放{recorded >= maxReplays ? " · 已满足 3 次连续成功" : ""}</small>
      </div>

      {replayError && <small className="profile-replay-error">{replayError}</small>}
      {profile.quick_verification && (
        <small style={{ color: "var(--text-3)", fontSize: 11 }}>
          快速复验：{profile.quick_verification.passed ? "通过" : "未通过"}
          {profile.quick_verification.reason ? ` · ${profile.quick_verification.reason}` : ""}
        </small>
      )}
    </article>
  );
}
