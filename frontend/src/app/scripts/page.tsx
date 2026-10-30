"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, Users } from "lucide-react";
import { anonymousLogin, ensureSession, saveToken } from "@/services/auth";
import { createSession, listReleases } from "@/services/play";
import { errorCode, http, isSessionExpired } from "@/lib/http";
import { newKey } from "@/lib/id";
import type { Release } from "@/types/api";
import { SiteHeader, SiteFooter } from "@/components/brand/SiteHeader";
import { TrialNotice } from "@/components/play/TrialNotice";

export default function ScriptsPage() {
  const router = useRouter();
  const [releases, setReleases] = useState<Release[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [character, setCharacter] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadFailed, setLoadFailed] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [query, setQuery] = useState("");
  const pending = useRef<{
    release: number;
    character: string;
    key: string;
  } | null>(null);
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setLoadFailed(false);
    setSessionExpired(false);
    try {
      setReleases(await listReleases());
    } catch (err) {
      setLoadFailed(true);
      setSessionExpired(isSessionExpired(err));
      setError(errorCode(err));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    const timer = setTimeout(() => void load(), 0);
    return () => clearTimeout(timer);
  }, [load]);
  const release = releases.find((r) => r.id === selected);
  const hasPublicPreview = releases.some((r) => r.public_preview);
  const browseAsGuest = async () => {
    if (busy) return;
    setBusy(true);
    try {
      // Switching identities is explicit; keep the old credentials if login fails.
      saveToken(await anonymousLogin());
      setSelected(null);
      setCharacter("");
      await load();
    } catch (err) {
      setError(errorCode(err));
      setSessionExpired(isSessionExpired(err));
    } finally {
      setBusy(false);
    }
  };
  const start = async () => {
    if (!release || !character || busy) return;
    setBusy(true);
    setError("");
    setSessionExpired(false);
    try {
      await ensureSession();
      // A protected read renews an expired access token before the one-shot
      // create. Browsing the public catalogue never creates an identity.
      await http.get("/api/auth/me");
      const storageKey = `opening:${release.id}:${character}`;
      if (
        !pending.current ||
        pending.current.release !== release.id ||
        pending.current.character !== character
      ) {
        const key = sessionStorage.getItem(storageKey) ?? newKey();
        pending.current = { release: release.id, character, key };
        sessionStorage.setItem(storageKey, key);
      }
      const opening = await createSession({
        release_id: release.id,
        character_id: character,
        idempotency_key: pending.current.key,
      });
      // Keep the key until the opening's play is resolved. A reload or retry reuses it.
      router.push(
        `/play?opening_session_id=${encodeURIComponent(opening.session_id)}`,
      );
    } catch (err) {
      setSessionExpired(isSessionExpired(err));
      setError(errorCode(err));
      setBusy(false);
    }
  };
  return (
    <>
      <SiteHeader section="scripts" />
      <main className="archive-page">
        <div className="archive-page-title">
          <p className="eyebrow">
            <span />
            STORY COLLECTION / 剧本档案
          </p>
          <h1>未启封的故事，等你入局。</h1>
          <p>
            {hasPublicPreview
              ? "从一份剧本开始，以一个角色的目光，走进迷雾深处。公开试玩可阅读私本、搜证与体验结局，AI 对话未开启。"
              : "从一份剧本开始，以一个角色的目光，走进迷雾深处。其余角色，由 AI 为你演绎。"}
          </p>
        </div>
        <label className="mt-8 block max-w-md text-sm">
          搜索剧本
          <input
            className="field mt-2"
            type="search"
            placeholder="输入剧本名称"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        {error && (
          <div className="panel mt-6 text-sm text-danger" role="alert">
            <p>{error}</p>
            {sessionExpired && (
              <p className="mt-2 text-sm text-muted">
                {loadFailed
                  ? "登录原账号可继续使用已有记录；也可以选择访客身份浏览剧本。"
                  : "请重新登录原账号后继续。"}
              </p>
            )}
            <div className="mt-4 flex flex-wrap gap-3">
              {sessionExpired || loadFailed ? (
                <>
                  <Link className="btn-primary" href="/player-login?next=%2Fscripts">
                    登录玩家账号
                  </Link>
                  {!sessionExpired && (
                    <button className="btn-secondary" disabled={busy} onClick={() => void load()}>
                      重新加载
                    </button>
                  )}
                  {loadFailed && sessionExpired && (
                    <button
                      className="btn-secondary"
                      disabled={busy}
                      onClick={() => void browseAsGuest()}
                    >
                      {busy ? "正在恢复访问…" : "以访客身份浏览"}
                    </button>
                  )}
                </>
              ) : (
                <button
                  disabled={busy}
                  className="btn-secondary"
                  onClick={() => void load()}
                >
                  重新加载
                </button>
              )}
            </div>
          </div>
        )}
        {loading ? (
          <p className="mt-10" role="status">
            正在读取剧本库…
          </p>
        ) : loadFailed ? null : !releases.length ? (
          <div className="archive-empty mt-10">
            <BookOpen className="text-muted" />
            <h2 className="mt-4 text-lg text-ink">新的案卷，尚未启封</h2>
            <p className="mt-2 text-sm text-muted">
              故事正在准备中。新剧本完成审核后，会在这里与你见面。
            </p>
          </div>
        ) : (
          <div className="mt-8 grid grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              {releases
                .filter((r) =>
                  r.title.toLowerCase().includes(query.toLowerCase()),
                )
                .map((r) => (
                  <button
                    key={r.id}
                    disabled={busy}
                    aria-pressed={selected === r.id}
                    className={`panel dossier-case min-h-52 text-left transition-colors ${selected === r.id ? "border-accent" : "hover:border-strong-line"}`}
                    onClick={() => {
                      setSelected(r.id);
                      setCharacter("");
                    }}
                  >
                    <span className="font-mono text-xs text-muted">
                      卷宗 {String(r.id).padStart(3, "0")}
                    </span>
                    {r.public_preview && (
                      <span className="ml-3 inline-block border border-accent/40 px-2 py-1 text-xs text-accent">
                        公开试玩
                      </span>
                    )}
                    {!r.public_preview && r.local_trial && (
                      <span className="ml-3 inline-block border border-accent/40 px-2 py-1 text-xs text-accent">
                        {r.local_trial.label}
                      </span>
                    )}
                    <h2 className="mt-4 text-xl text-ink">{r.title}</h2>
                    <p className="mt-4 flex items-center gap-2 text-sm text-muted">
                      <Users size={15} />
                      {r.player_count} 个角色 · 单人游玩
                    </p>
                    <p className="mt-2 text-xs text-muted">
                      版次 {r.content_version}
                    </p>
                    {r.public_preview && (
                      <p className="mt-2 text-xs leading-6 text-muted">
                        原创测试剧本 · 未完成模型审核 · AI 对话关闭
                      </p>
                    )}
                  </button>
                ))}
              {!releases.some((r) =>
                r.title.toLowerCase().includes(query.toLowerCase()),
              ) && <p className="text-sm text-muted">没有找到匹配的剧本。</p>}
            </div>
            <section className="panel space-y-5 lg:sticky lg:top-8">
              <h2 className="text-xl text-ink">
                {release ? "选择你的角色" : "先选择一个故事"}
              </h2>
              {release && (
                <>
                  <p className="text-sm text-muted">{release.title}</p>
                  {release.public_preview && (
                    <TrialNotice publicPreview={release.public_preview} />
                  )}
                  {!release.public_preview && release.local_trial && (
                    <p className="border-l-2 border-accent/50 pl-3 text-xs leading-6 text-muted">
                      {release.local_trial.notice}
                    </p>
                  )}
                  <fieldset className="space-y-2">
                    <legend className="sr-only">扮演角色</legend>
                    {release.characters.map((c) => (
                      <label
                        key={c.id}
                        className={`flex cursor-pointer items-center gap-3 rounded-md border p-3 text-sm ${character === c.id ? "border-accent text-ink" : "border-line"}`}
                      >
                        <input
                          type="radio"
                          name="character"
                          value={c.id}
                          checked={character === c.id}
                          disabled={busy}
                          onChange={() => setCharacter(c.id)}
                        />
                        {c.name}
                      </label>
                    ))}
                  </fieldset>
                  <p className="text-xs leading-relaxed text-muted">
                    选角后将读取该角色获准知道的材料。进入后可保存进度并继续。
                  </p>
                  <button
                    className="btn-primary w-full"
                    disabled={!character || busy}
                    onClick={() => void start()}
                  >
                    {busy ? "正在进入…" : "以此角色开始"}
                  </button>
                </>
              )}
            </section>
          </div>
        )}
      </main>
      <SiteFooter />
    </>
  );
}
