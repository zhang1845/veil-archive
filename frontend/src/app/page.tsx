"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Fingerprint,
  MessageCircle,
  FolderOpen,
  MoveUpRight,
} from "lucide-react";
import { hasToken } from "@/services/auth";
import { listLibrary, listReleases } from "@/services/play";
import type { LibraryItem, PublicPreview } from "@/types/api";
import { SiteHeader, SiteFooter } from "@/components/brand/SiteHeader";
import { ArchiveArtwork } from "@/components/brand/ArchiveArtwork";
import { TrialNotice } from "@/components/play/TrialNotice";

const steps = [
  {
    number: "01",
    title: "进入案发现场",
    text: "选定剧本与身份，读懂属于你的秘密。",
    icon: BookOpen,
  },
  {
    number: "02",
    title: "交叉核对证词",
    text: "调查物证，盘问 AI 角色，找出说法中的矛盾。",
    icon: MessageCircle,
  },
  {
    number: "03",
    title: "还原最后真相",
    text: "连接每一条线索，作出你的最终判断。",
    icon: Fingerprint,
  },
];

export default function HomePage() {
  const [recent, setRecent] = useState<LibraryItem | null>(null);
  const [count, setCount] = useState<number | null>(null);
  const [publicPreview, setPublicPreview] = useState<PublicPreview>();
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [library, catalog] = await Promise.allSettled([
          hasToken() ? listLibrary(0, 1) : Promise.resolve(null),
          listReleases(),
        ]);
        if (!cancelled) {
          if (library.status === "fulfilled") {
            setRecent(library.value?.items[0] ?? null);
          }
          if (catalog.status === "fulfilled") {
            setCount(catalog.value.length);
            setPublicPreview(catalog.value.find((release) => release.public_preview)?.public_preview);
          }
        }
      } catch {
        /* Navigation remains available if the catalog cannot be read. */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="home-page">
      <SiteHeader section="home" />
      <main>
        <section className="archive-hero">
          <div className="scene-backdrop" aria-hidden="true" />
          <div className="scene-grain" aria-hidden="true" />
          <div className="hero-inner">
            <div className="hero-copy">
              <p className="eyebrow">
                <span />
                SINGLE PLAYER / 单人沉浸式 AI 剧本杀
              </p>
              <h1>
                他们都在说谎。
                <br />
                <span>
                  真相，<em>等你审问。</em>
                </span>
              </h1>
              <p className="hero-description">
                一扇未关的门，一句对不上的证词。
                <br />
                {publicPreview
                  ? "选一个身份走进迷局，核对每一份封存口供。"
                  : "选一个身份走进迷局，与 AI 角色交锋。"}
                <br />
                在所有人掩藏的秘密里，找到最后一块拼图。
              </p>
              <TrialNotice publicPreview={publicPreview} />
              <div className="hero-actions">
                <Link href="/scripts" className="btn-primary hero-primary">
                  进入剧本档案
                  <ArrowUpRight size={18} aria-hidden />
                </Link>
                <Link
                  href="/guide"
                  className="btn-secondary"
                  aria-label="新手教程，了解如何游玩"
                >
                  <BookOpen size={16} aria-hidden />
                  新手教程
                </Link>
                <Link href="/records" className="hero-secondary">
                  查看调查手记
                  <ArrowRight size={16} aria-hidden />
                </Link>
              </div>
              <div className="hero-footnote">
                <Fingerprint size={16} aria-hidden />
                <span>
                  {publicPreview
                    ? "你掌握一个角色的秘密。通过搜证，还原其他角色的证词。"
                    : "你掌握一个角色的秘密。其余角色，由 AI 演绎。"}
                </span>
              </div>
            </div>
            <ArchiveArtwork />
          </div>
          <div className="scene-bottom">
            <span>
              THE VEIL ARCHIVE <i /> 每个秘密，都有破绽。
            </span>
            <span>
              向下 · 继续调查 <ArrowRight size={13} aria-hidden />
            </span>
          </div>
        </section>
        <div className="home-main">
          <section className="story-steps" aria-label="如何游玩">
            {steps.map((step) => (
              <article key={step.number}>
                <span className="step-number">{step.number}</span>
                <div>
                  <h2>
                    <step.icon size={17} aria-hidden />
                    {step.title}
                  </h2>
                  <p>
                    {publicPreview && step.number === "02"
                      ? "调查物证，阅读封存口供，找出说法中的矛盾。"
                      : step.text}
                  </p>
                </div>
              </article>
            ))}
          </section>
          <section className="personal-archive">
            <div className="section-heading">
              <div>
                <p className="eyebrow">INVESTIGATION LOG</p>
                <h2>你的调查档案</h2>
              </div>
              <Link href="/records">
                查看全部手记
                <ArrowUpRight size={16} aria-hidden />
              </Link>
            </div>
            {recent ? (
              <Link
                href={`/play?play_id=${recent.play_id}`}
                className="recent-dossier"
              >
                <span className="recent-icon">
                  <FolderOpen size={28} aria-hidden />
                </span>
                <div>
                  <p className="dossier-kicker">
                    {recent.settled ? "已归档" : "上次读到这里"} ·{" "}
                    {recent.phase_label}
                  </p>
                  <h3>{recent.title}</h3>
                  <p>你扮演 {recent.character_name}</p>
                </div>
                <span className="recent-resume">
                  {recent.settled ? "回看结局" : "续写这一局"}
                  <ArrowRight size={18} aria-hidden />
                </span>
              </Link>
            ) : (
              <div className="empty-dossier">
                <FolderOpen size={32} strokeWidth={1} aria-hidden />
                <div>
                  <h3>调查，始于你的第一次怀疑。</h3>
                  <p>尚无调查记录。进入剧本后，你的线索与进度会留在这里。</p>
                </div>
                <Link href="/scripts" aria-label="前往剧本档案">
                  <MoveUpRight size={24} aria-hidden />
                </Link>
              </div>
            )}
          </section>
          <div className="archive-bottom-note">
            <span>别急着相信任何人。先听完他们的证词。</span>
            <Link href="/scripts">
              {count === null
                ? "探索剧本档案"
                : count > 0
                  ? `${count} 份剧本档案，等待调查`
                  : "新案卷完成审核后将在这里开放"}
              <ArrowRight size={13} aria-hidden />
            </Link>
          </div>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}
