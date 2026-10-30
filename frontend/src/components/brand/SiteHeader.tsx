import Link from "next/link";
import { ArrowUpRight, Feather } from "lucide-react";
import { Brand } from "./Brand";

type Section = "home" | "scripts" | "records" | "studio";
export function SiteHeader({ section }: { section?: Section }) {
  return (
    <header className="site-header">
      <div className="site-header-inner">
        <Brand />
        <nav aria-label="主导航" className="site-navigation">
          <Link
            href="/scripts"
            aria-current={section === "scripts" ? "page" : undefined}
          >
            剧本档案
          </Link>
          <Link
            href="/records"
            aria-current={section === "records" ? "page" : undefined}
          >
            我的手记
          </Link>
          <Link
            href="/admin"
            className="studio-link"
            aria-current={section === "studio" ? "page" : undefined}
          >
            <Feather size={15} aria-hidden />
            剧本工作台
            <ArrowUpRight size={13} aria-hidden />
          </Link>
        </nav>
      </div>
    </header>
  );
}
export function SiteFooter() {
  return (
    <footer className="site-footer">
      <span>雾幕档案</span>
      <p>迷雾终会散去，秘密不会。</p>
      <span className="font-mono">THE VEIL ARCHIVE · 2026</span>
    </footer>
  );
}
