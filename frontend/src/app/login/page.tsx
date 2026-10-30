"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Feather } from "lucide-react";
import { passwordLogin } from "@/services/auth";
import { errorCode } from "@/lib/http";
import { SiteHeader, SiteFooter } from "@/components/brand/SiteHeader";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <>
      <SiteHeader section="studio" />
      <main className="login-layout">
        <aside className="login-story">
          <div>
            <p className="eyebrow">BEHIND THE STORY</p>
            <h2>
              替每个角色，
              <br />
              藏好一个秘密。
            </h2>
            <p>
              从一颗怀疑的种子开始，编织人物、埋下线索，再把一份值得推敲的故事，交给下一位入局者。
            </p>
          </div>
          <div className="login-story-bottom">
            <span>雾</span>
            <span>创作 · 审阅 · 归档</span>
          </div>
        </aside>
        <section className="login-form-panel">
          <p className="eyebrow">
            <Feather size={14} aria-hidden />
            CREATOR ACCESS
          </p>
          <h1>创作者登录</h1>
          <p className="login-note">登录管理员账号，进入雾幕档案剧本工作台。</p>
          <form
            className="space-y-5"
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setError("");
              try {
                await passwordLogin(username, password, true);
                router.replace("/admin");
              } catch (err) {
                setError(errorCode(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            <label className="block">
              管理员账号
              <input
                required
                autoComplete="username"
                placeholder="输入管理账号"
                className="field mt-2"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
            <label className="block">
              密码
              <input
                required
                type="password"
                autoComplete="current-password"
                placeholder="输入密码"
                className="field mt-2"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            {error && (
              <p role="alert" className="text-sm text-danger">
                {error}
              </p>
            )}
            <button className="btn-primary" disabled={busy}>
              {busy ? "正在核验身份…" : "进入剧本工作台"}
              <ArrowRight size={16} aria-hidden />
            </button>
          </form>
          <p className="login-form-footer">
            仅限获授权的创作者访问，未登录不加载工作台内容。
          </p>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}
