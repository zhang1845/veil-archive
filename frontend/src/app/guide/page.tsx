import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeft, ArrowUpRight, BookOpen } from "lucide-react";
import { SiteFooter, SiteHeader } from "@/components/brand/SiteHeader";
import { PlayGuide } from "@/components/play/PlayGuide";

export const metadata: Metadata = {
  title: "新手教程",
  description: "从选角、阅读私本、地点搜证到结局复盘，了解雾幕档案的游玩顺序。",
};

export default function GuidePage() {
  return (
    <>
      <SiteHeader />
      <main className="archive-page">
        <Link
          href="/"
          className="inline-flex min-h-11 items-center gap-2 text-sm text-muted hover:text-ink"
        >
          <ArrowLeft size={16} aria-hidden />
          返回首页
        </Link>
        <header className="archive-page-title">
          <p className="eyebrow">
            <span />
            FIELD GUIDE / 新手入局
          </p>
          <h1>第一次入局，从这里开始。</h1>
          <p>
            先了解操作顺序，再亲自走进案件。这里介绍选角、搜证与结算的方法，保留你的独立推理。
          </p>
        </header>
        <section
          className="panel mt-8 flex flex-wrap items-center justify-between gap-6 border-accent/40"
          aria-labelledby="first-case-title"
        >
          <div className="min-w-0 flex-1 basis-64">
            <p className="flex items-center gap-2 text-xs tracking-wide text-accent">
              <BookOpen size={16} aria-hidden />
              推荐第一次体验
            </p>
            <h2
              id="first-case-title"
              className="mt-3 text-2xl leading-9 text-ink"
            >
              雨停之前的第七码
            </h2>
            <p className="mt-2 text-sm leading-7 text-muted">
              4个角色、4处地点，首次建议选择“沈雾”。按照下方顺序，体验阅读、调查和结局复盘。
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <Link href="/scripts" className="btn-primary">
              去选剧本，开始体验
              <ArrowUpRight size={16} aria-hidden />
            </Link>
            <a href="#tutorial-steps" className="btn-secondary">
              先看操作顺序
            </a>
          </div>
        </section>
        <PlayGuide />
        <div className="mt-10 flex flex-wrap items-center justify-between gap-5 border-t border-line pt-7">
          <p className="text-sm leading-7 text-muted">
            准备好后，选一份案卷，留下你的第一份调查记录。
          </p>
          <Link href="/scripts" className="btn-primary">
            我知道怎么玩了，去剧本库
            <ArrowUpRight size={16} aria-hidden />
          </Link>
        </div>
      </main>
      <SiteFooter />
    </>
  );
}
