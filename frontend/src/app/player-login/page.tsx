"use client";

import Link from "next/link";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, KeyRound } from "lucide-react";
import { SiteHeader, SiteFooter } from "@/components/brand/SiteHeader";
import { passwordLogin } from "@/services/auth";
import { errorCode } from "@/lib/http";
import { playerReturnPath } from "@/lib/player-login";

export default function PlayerLoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  return (
    <>
      <SiteHeader />
      <main className="login-layout">
        <aside className="login-story">
          <div>
            <p className="eyebrow">ENTER THE ARCHIVE</p>
            <h2>带着你的身份，<br />走进迷雾。</h2>
            <p>一份案卷，一个角色。登录后挑选故事，继续属于你的调查。</p>
          </div>
          <div className="login-story-bottom"><span>雾</span><span>阅读 · 搜证 · 推理</span></div>
        </aside>
        <section className="login-form-panel">
          <p className="eyebrow"><KeyRound size={14} aria-hidden />PLAYER ACCESS</p>
          <h1>玩家登录</h1>
          <p className="login-note">使用管理员分配给你的独立账号。游玩进度会保存在这个账号下。</p>
          <form className="space-y-5" onSubmit={async (event) => {
            event.preventDefault();
            if (busy) return;
            setBusy(true);
            setError("");
            try {
              await passwordLogin(username, password, false);
              const next = new URLSearchParams(window.location.search).get("next");
              router.replace(playerReturnPath(next));
            } catch (cause) {
              setError(errorCode(cause));
            } finally {
              setBusy(false);
            }
          }}>
            <label className="block">账号
              <input required autoComplete="username" className="field mt-2" placeholder="输入玩家账号或邮箱" value={username} onChange={(event) => setUsername(event.target.value)} />
            </label>
            <label className="block">密码
              <input required type="password" autoComplete="current-password" className="field mt-2" placeholder="输入密码" value={password} onChange={(event) => setPassword(event.target.value)} />
            </label>
            {error && <p role="alert" className="text-sm text-danger">{error}</p>}
            <button className="btn-primary" disabled={busy}>{busy ? "正在登录…" : "进入剧本档案"}<ArrowRight size={16} aria-hidden /></button>
          </form>
          <p className="login-form-footer">尚未取得账号，请联系本网站管理员。<Link className="ml-2 underline" href="/login">创作者登录</Link></p>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}
