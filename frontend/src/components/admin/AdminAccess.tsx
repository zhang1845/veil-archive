"use client";

import Link from "next/link";
import { Brand } from "@/components/brand/Brand";
import { useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { hasAdminSession } from "@/services/auth";
import { errorCode } from "@/lib/http";

export default function AdminAccess({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [allowed, setAllowed] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    void hasAdminSession()
      .then((authorized) => {
        if (!active) return;
        if (authorized) setAllowed(true);
        else router.replace("/login");
      })
      .catch((reason) => {
        if (active) setError(errorCode(reason));
      });
    return () => {
      active = false;
    };
  }, [router, attempt]);

  if (allowed) return children;
  return (
    <main className="mx-auto max-w-md space-y-8 px-6 py-20">
      <Brand />
      {error ? (
        <>
          <h1 className="text-xl text-ink">暂时无法验证登录状态</h1>
          <p role="alert" className="mt-3 text-sm text-muted">
            {error}
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <button
              className="btn-primary"
              onClick={() => {
                setError("");
                setAttempt((value) => value + 1);
              }}
            >
              重新验证
            </button>
            <Link className="btn-secondary" href="/login">
              前往登录
            </Link>
          </div>
        </>
      ) : (
        <p role="status" className="text-sm text-muted">
          正在验证管理员登录…
        </p>
      )}
    </main>
  );
}
