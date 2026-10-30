"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { BookOpen, Flag, RefreshCw } from "lucide-react";
import { usePlaySessionStore } from "@/stores/playSessionStore";
import type { Material } from "@/types/api";
import { SiteHeader } from "@/components/brand/SiteHeader";

export function PlayRoom() {
  const router = useRouter();
  const search = useSearchParams();
  const playId = search.get("play_id");
  const openingSessionId = search.get("opening_session_id");
  const {
    mode,
    title,
    play,
    error,
    bootstrap,
    characters,
    busy,
    pending,
    refresh,
    retryPending,
    performAction,
    speakText,
    askQuestion,
  } = usePlaySessionStore();
  const [channel, setChannel] = useState("ask");
  const [target, setTarget] = useState("");
  const [text, setText] = useState("");
  const [confirmAdvance, setConfirmAdvance] = useState(false);

  useEffect(() => {
    if (!playId && !openingSessionId) {
      router.replace("/scripts");
      return;
    }
    void bootstrap({
      playId: playId ?? undefined,
      openingSessionId: openingSessionId ?? undefined,
    });
  }, [playId, openingSessionId, bootstrap, router]);
  useEffect(() => {
    if (!play || playId === play.play_id) return;
    router.replace(`/play?play_id=${encodeURIComponent(play.play_id)}`);
  }, [play, playId, router]);
  const disabled = busy || !!pending;
  const activeChannel = play?.local_trial ? "speak" : channel;
  const others = characters.filter((c) => c.id !== play?.selected_character_id);
  const selectedTarget = others.some((c) => c.id === target) ? target : "";
  const myName =
    characters.find((c) => c.id === play?.selected_character_id)?.name ?? "你";

  if (mode !== "live" || !play)
    return (
      <main className="mx-auto max-w-xl px-6 py-20">
        <Link href="/scripts" className="text-sm text-muted">
          ← 返回剧本库
        </Link>
        <h1 className="mt-8 text-2xl text-ink">
          {mode === "error" ? "未能进入本局" : "正在读取你的故事…"}
        </h1>
        {error && (
          <p role="alert" className="mt-4 break-words text-sm text-danger">
            {error}
          </p>
        )}
        {mode === "error" && (
          <button
            className="btn-primary mt-6"
            onClick={() =>
              void bootstrap({
                playId: playId ?? undefined,
                openingSessionId: openingSessionId ?? undefined,
              })
            }
          >
            重试读取
          </button>
        )}
      </main>
    );

  const materials = (
    label: string,
    items: Material[] | undefined,
    collection: string,
  ) => (
    <section className="panel">
      <h3 className="flex items-center gap-2 text-base text-ink">
        <BookOpen size={16} />
        {label}
      </h3>
      {!items?.length ? (
        <p className="mt-3 text-sm text-muted">当前没有已解锁的材料。</p>
      ) : (
        <div className="mt-4 space-y-4">
          {items.map((item) => (
            <article
              key={item.id}
              className="border-t border-line pt-3 first:border-0 first:pt-0"
            >
              <p className="whitespace-pre-wrap text-sm leading-7 text-body">
                {item.text}
              </p>
              {item.kind && (
                <p className="mt-1 text-xs text-muted">
                  {(
                    {
                      FACT: "本人已知事实",
                      CLAIM: "角色说法",
                      INFERENCE: "角色推测",
                    } as Record<string, string>
                  )[item.kind] ?? item.kind}
                </p>
              )}
              {item.can_share && (
                <button
                  className="btn-secondary mt-3"
                  disabled={disabled || play.settled}
                  onClick={() =>
                    void performAction("SHARE_MATERIAL", {
                      collection,
                      id: item.id,
                    })
                  }
                >
                  公开这条材料
                </button>
              )}
              {item.disclosure === "KEEP_PRIVATE" && (
                <p className="mt-2 text-xs text-muted">仅本人可见 · 不可公开</p>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );

  return (
    <div className="reading-room min-h-screen">
      <SiteHeader />
      <header className="room-toolbar px-5 py-5">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4">
          <div>
            <Link href="/" className="text-xs text-muted">
              ← 返回首页 · 进度已保存
            </Link>
            <h1 className="mt-2 text-xl text-ink">{title}</h1>
            <p className="mt-1 text-sm text-muted">
              你是 {myName} ·{" "}
              {play.current_phase?.title ?? play.phase?.title ?? "当前阶段"}
              {play.settled && " · 已结算"}
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <button
              className="btn-secondary"
              disabled={busy}
              onClick={() => void refresh()}
            >
              <RefreshCw size={15} />
              刷新进度
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl space-y-6 px-4 py-6 sm:px-8">
        {play.local_trial && (
          <aside
            className="panel border-accent/40 text-sm leading-7"
            role="note"
          >
            <strong className="text-accent">{play.local_trial.label}</strong>
            <p className="mt-1 text-muted">{play.local_trial.notice}</p>
          </aside>
        )}
        {error && (
          <div
            role="alert"
            className="panel border-danger/40 text-sm text-danger"
          >
            {error}
          </div>
        )}
        {pending && (
          <div className="panel space-y-3">
            <p className="text-sm text-muted">
              上次操作结果尚未确认。核对原请求后再继续，避免重复扣费或推进。
            </p>
            <button
              className="btn-secondary"
              disabled={busy}
              onClick={() => void retryPending()}
            >
              核对原请求
            </button>
          </div>
        )}
        {busy && (
          <p role="status" className="text-sm text-accent">
            正在处理，请稍候…
          </p>
        )}
        <section id="play-opening" className="panel scroll-mt-6">
          <h2 className="text-lg text-ink">故事开场</h2>
          <p className="mt-4 whitespace-pre-wrap text-sm leading-7 text-body">
            {play.introduction?.text}
          </p>
        </section>
        <div
          id="play-materials"
          className="grid scroll-mt-6 grid-cols-1 items-start gap-6 lg:grid-cols-2"
        >
          <div className="space-y-5">
            {materials("你的私本", play.private_knowledge, "knowledge")}
            {materials("你的私有证据", play.private_evidence, "evidence")}
          </div>
          <div className="space-y-5">
            {materials("公开信息", play.public_knowledge, "knowledge")}
            {materials("公开物证", play.public_evidence, "evidence")}
          </div>
        </div>
        {!play.settled && play.mechanics && (
          <section id="play-investigation" className="panel scroll-mt-6">
            <h2 className="text-lg text-ink">
              调查地点 · 剩余 {play.mechanics.remaining_points} 点
            </h2>
            <div className="mt-4 flex flex-wrap gap-3">
              {play.mechanics.available_actions.map((a) => (
                <button
                  key={a.id}
                  disabled={disabled}
                  className="btn-secondary"
                  onClick={() =>
                    void performAction("PERFORM_ACTION", { action_id: a.id })
                  }
                >
                  {a.label} · {a.cost} 点
                </button>
              ))}
              {!play.mechanics.available_actions.length && (
                <p className="text-sm text-muted">本阶段暂无可用调查行动。</p>
              )}
            </div>
          </section>
        )}
        <section id="play-discussion" className="panel scroll-mt-6">
          <h2 className="text-lg text-ink">交流记录</h2>
          <div className="mt-4 space-y-4">
            {play.discussion?.entries.map((entry) => (
              <article key={entry.id} className="border-l-2 border-accent pl-4">
                <p className="text-xs text-accent">
                  {characters.find((c) => c.id === entry.speaker)?.name ??
                    entry.speaker}{" "}
                  · 公开发言
                </p>
                <p className="mt-1 whitespace-pre-wrap text-sm leading-7">
                  {entry.text}
                </p>
              </article>
            ))}
            {play.dialogue?.map((entry, i) => (
              <article
                key={`dialogue-${i}`}
                className="border-l-2 border-line pl-4"
              >
                <p className="text-xs text-muted">
                  {entry.character_name ??
                    characters.find((c) => c.id === entry.character_id)?.name ??
                    entry.speaker ??
                    "角色回复"}
                </p>
                <p className="mt-1 whitespace-pre-wrap text-sm leading-7">
                  {entry.text}
                </p>
              </article>
            ))}
            {!play.discussion?.entries.length && !play.dialogue?.length && (
              <p className="text-sm text-muted">
                {play.local_trial
                  ? "还没有交流记录。搜证可取得封存口供，也可以在下方记录你的推理。"
                  : "还没有交流记录。阅读私本后，可以向其他角色询问线索。"}
              </p>
            )}
          </div>
          {!play.settled && (
            <form
              className="mt-6 space-y-4 border-t border-line pt-5"
              onSubmit={async (e) => {
                e.preventDefault();
                if (!text.trim()) return;
                const ok =
                  activeChannel === "ask"
                    ? await askQuestion(selectedTarget, text.trim())
                    : await speakText(text.trim());
                if (ok) setText("");
              }}
            >
              <div className="flex flex-wrap gap-3">
                <label className="text-sm">
                  交流方式
                  <select
                    className="field mt-2"
                    value={activeChannel}
                    disabled={disabled}
                    onChange={(e) => setChannel(e.target.value)}
                  >
                    <option value="ask" disabled={!!play.local_trial}>
                      向角色询问
                    </option>
                    <option value="speak">公开发言</option>
                  </select>
                </label>
                {activeChannel === "ask" && (
                  <label className="text-sm">
                    询问对象
                    <select
                      required
                      className="field mt-2"
                      value={selectedTarget}
                      disabled={disabled}
                      onChange={(e) => setTarget(e.target.value)}
                    >
                      <option value="" disabled>
                        选择角色
                      </option>
                      {others.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
              </div>
              {activeChannel === "ask" && !play.model?.available && (
                <p className="text-sm text-muted">
                  角色模型暂不可用 · {play.model?.reason ?? "请检查配置"}
                  。仍可阅读、分享材料和推进。
                </p>
              )}
              <label className="block text-sm">
                {activeChannel === "ask" ? "你想询问什么？" : "你的公开发言"}
                <textarea
                  required
                  maxLength={1000}
                  rows={3}
                  className="field mt-2"
                  disabled={disabled}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  placeholder="结合当前已知的材料展开交流…"
                />
              </label>
              <button
                className="btn-primary"
                disabled={
                  disabled ||
                  !text.trim() ||
                  (activeChannel === "ask" &&
                    (!selectedTarget || !play.model?.available))
                }
              >
                {busy
                  ? "处理中…"
                  : activeChannel === "ask"
                    ? "发送问题"
                    : "公开发言"}
              </button>
              <p className="text-xs text-muted">
                {play.local_trial
                  ? "公开发言会保存到本局。搜齐四处地点后，可推进到终局并揭晓真相。"
                  : "公开发言会保存到本局；向角色询问会调用模型，按本局预算计费。"}
              </p>
            </form>
          )}
        </section>
        {play.settlement && (
          <section
            id="play-ending"
            className="panel scroll-mt-6 border-accent/40"
          >
            <h2 className="flex items-center gap-2 text-xl text-accent">
              <Flag size={20} />
              真相揭晓
            </h2>
            <p className="mt-4 whitespace-pre-wrap text-sm leading-7">
              {play.settlement.text}
            </p>
            {play.settlement.truths.map((t) => (
              <p
                key={t.id}
                className="mt-4 whitespace-pre-wrap border-t border-line pt-4 text-sm leading-7"
              >
                {t.text}
              </p>
            ))}
            <Link href="/scripts" className="btn-primary mt-6">
              选择下一个故事
            </Link>
          </section>
        )}
        {!play.settled && (
          <section id="play-progress" className="panel scroll-mt-6">
            <h2 className="text-lg text-ink">阶段进度</h2>
            <p className="mt-2 text-sm text-muted">
              先读完已获材料并完成交流。推进后会按剧本规则解锁下一阶段。
            </p>
            {confirmAdvance ? (
              <div className="mt-4 space-y-3">
                <p className="text-sm text-ink">
                  {play.phase_complete
                    ? "确认结束本局并揭晓真相？"
                    : "确认进入下一阶段？"}
                </p>
                <div className="flex flex-wrap gap-3">
                  <button
                    disabled={disabled}
                    className="btn-primary"
                    onClick={async () => {
                      if (
                        await performAction(
                          play.phase_complete ? "SETTLE" : "ADVANCE_PHASE",
                        )
                      )
                        setConfirmAdvance(false);
                    }}
                  >
                    确认{play.phase_complete ? "结算" : "推进"}
                  </button>
                  <button
                    className="btn-secondary"
                    onClick={() => setConfirmAdvance(false)}
                  >
                    继续留在本阶段
                  </button>
                </div>
              </div>
            ) : (
              <button
                className="btn-primary mt-4"
                disabled={
                  disabled || (!play.can_advance && !play.phase_complete)
                }
                onClick={() => setConfirmAdvance(true)}
              >
                {play.phase_complete ? "结束并揭晓真相" : "进入下一阶段"}
              </button>
            )}
          </section>
        )}
      </main>
    </div>
  );
}
