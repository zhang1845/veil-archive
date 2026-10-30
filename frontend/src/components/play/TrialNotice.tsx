import type { LocalTrial, PublicPreview } from "@/types/api";

export function TrialNotice({
  publicPreview,
  localTrial,
}: {
  publicPreview?: PublicPreview;
  localTrial?: LocalTrial;
}) {
  if (!publicPreview && !localTrial) return null;
  const notice = publicPreview ?? localTrial!;
  return (
    <aside className="panel border-accent/40 text-sm leading-7" role="note">
      <strong className="text-accent">
        {publicPreview ? "公开试玩 · 原创测试剧本" : notice.label}
      </strong>
      <p className="mt-1 text-muted">{notice.notice}</p>
      {publicPreview && (
        <p className="mt-1 text-xs text-muted">
          尚未完成正式模型审核 · AI 对话未开启
        </p>
      )}
    </aside>
  );
}
