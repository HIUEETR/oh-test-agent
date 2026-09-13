// Profile 资产面板：快速复验、锁定、回退与失效（补齐后端已有但旧 UI 缺失的 invalidate 入口）。

import { useEffect } from "react";
import { Ban, Lock, RefreshCw, RotateCcw, ShieldCheck, Unlock } from "lucide-react";
import { Badge, EmptyState } from "../../components/ui/primitives";
import { useConsole, type ProfileAction } from "../../stores/console";
import type { ProfileSummary } from "../../api/types";

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
          <ProfileCard key={profile.profile_id} profile={profile} onAction={updateProfile} />
        ))}
        {profiles.length === 0 && (
          <EmptyState title="暂无 Profile" hint="探索运行完成后会自动沉淀 Profile 资产" icon={<ShieldCheck size={34} />} />
        )}
      </div>
    </section>
  );
}

function ProfileCard({ profile, onAction }: { profile: ProfileSummary; onAction: (profile: ProfileSummary, action: ProfileAction) => void }) {
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
        <button type="button" className="danger compact" onClick={() => onAction(profile, "invalidate")} disabled={profile.status !== "verified"}>
          <Ban size={13} />失效
        </button>
      </div>
      {profile.quick_verification && (
        <small style={{ color: "var(--text-3)", fontSize: 11 }}>
          快速复验：{profile.quick_verification.passed ? "通过" : "未通过"}
          {profile.quick_verification.reason ? ` · ${profile.quick_verification.reason}` : ""}
        </small>
      )}
    </article>
  );
}
