"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, NotebookPen } from "lucide-react";
import { ensureSession } from "@/services/auth";
import { listLibrary } from "@/services/play";
import type { LibraryItem } from "@/types/api";
import { SiteHeader, SiteFooter } from "@/components/brand/SiteHeader";

export default function RecordsPage() {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await ensureSession();
        const library = await listLibrary(0, 50);
        if (!cancelled) setItems(library.items);
      } catch {
        if (!cancelled) setOffline(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <>
      <SiteHeader section="records" />
      <main className="archive-page">
        <div className="archive-page-title">
          <p className="eyebrow">
            <span />
            PRIVATE NOTES / 我的手记
          </p>
          <h1>那些由你亲历的故事。</h1>
          <p>
            未完的调查、走过的分岔、揭晓的真相，都留在这里。随时翻开，回到你的故事里。
          </p>
        </div>
        {offline && (
          <p role="alert" className="mt-6 text-sm text-danger">
            暂时无法读取手记，请检查登录状态和后端连接。
            <Link href="/player-login?next=%2Frecords" className="ml-3 underline">
              重新登录原账号
            </Link>
          </p>
        )}
        {loading ? (
          <p role="status" className="mt-10 text-sm text-muted">
            正在整理你的手记…
          </p>
        ) : offline ? null : !items.length ? (
          <section className="archive-empty mt-8">
            <NotebookPen size={40} strokeWidth={1} aria-hidden />
            <h2>纸页空白，故事尚未开始。</h2>
            <p>当你进入第一份案卷，你的角色与进度就会记录在这里。</p>
            <Link href="/scripts" className="btn-primary mt-7">
              挑选第一份剧本
              <ArrowUpRight size={16} aria-hidden />
            </Link>
          </section>
        ) : (
          <ul className="mt-8 space-y-4">
            {items.map((item, index) => (
              <li key={item.play_id}>
                <Link
                  href={`/play?play_id=${item.play_id}`}
                  className="record-link flex flex-wrap items-center gap-5 border border-line px-6 py-6 transition-colors"
                >
                  <span className="font-mono text-xs text-muted">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-xs text-accent">
                      {item.settled ? "已结卷" : "调查中"} · {item.phase_label}
                    </p>
                    <h2 className="mt-2 font-display text-2xl">{item.title}</h2>
                    <p className="mt-2 text-xs text-muted">
                      你的角色 / {item.character_name}
                    </p>
                  </div>
                  <span className="flex items-center gap-3 text-xs text-accent">
                    {item.settled ? "回看故事" : "继续调查"}
                    <ArrowUpRight size={17} aria-hidden />
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </main>
      <SiteFooter />
    </>
  );
}
