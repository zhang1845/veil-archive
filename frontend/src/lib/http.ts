import axios, { type InternalAxiosRequestConfig } from "axios";
import { API_BASE_URL } from "./config";
import { accessToken, refreshToken, saveToken } from "./tokens";
import type { Token } from "@/types/api";

export const http = axios.create({ baseURL: API_BASE_URL });

type SessionRequest = InternalAxiosRequestConfig & { sessionRetried?: boolean };
const isAuthRequest = (url?: string) =>
  /^\/api\/auth\/(login|phone-login|anonymous-login|refresh|sms-code|register)(?:$|[/?])/.test(
    url ?? "",
  );

http.interceptors.request.use((config) => {
  // Authentication requests must not inherit the expired bearer being replaced.
  const token = isAuthRequest(config.url) ? null : accessToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  else config.headers.delete("Authorization");
  return config;
});

let refreshing: { refresh: string; promise: Promise<string> } | null = null;

async function renewAccess(
  rejectedAccess: string,
  refresh: string,
): Promise<string> {
  const current = accessToken();
  if (current && current !== rejectedAccess) return current;
  if (refreshing?.refresh === refresh) return refreshing.promise;

  const renew = async () => {
    // A second tab or a password login may already have replaced this identity.
    const latest = accessToken();
    if (latest && latest !== rejectedAccess) return latest;
    if (refreshToken() !== refresh)
      throw new Error("登录状态已变化，请重新加载。");
    const { data } = await http.post<Token>(
      "/api/auth/refresh",
      { refresh_token: refresh },
      { timeout: 10_000 },
    );
    const after = accessToken();
    if (after !== rejectedAccess || refreshToken() !== refresh) {
      if (after) return after;
      throw new Error("登录状态已变化，请重新登录。");
    }
    if (typeof data.access_token !== "string" || !data.access_token) {
      throw new Error("续期响应无效，请重新登录。");
    }
    saveToken(data);
    return data.access_token;
  };

  // Refresh tokens rotate once per use. Coordinate both concurrent reads and tabs.
  const promise = (async () => {
    if (typeof navigator !== "undefined" && navigator.locks) {
      return await navigator.locks.request(
        "veil-archive-session-refresh",
        renew,
      );
    }
    return await renew();
  })().finally(() => {
    if (refreshing?.promise === promise) refreshing = null;
  });
  refreshing = { refresh, promise };
  return promise;
}

http.interceptors.response.use(
  (response) => response,
  async (error: unknown) => {
    if (!axios.isAxiosError(error)) throw error;
    const config = error.config as SessionRequest | undefined;
    if (
      error.response?.status !== 401 ||
      !config ||
      config.sessionRetried ||
      isAuthRequest(config.url) ||
      !["get", "head"].includes(config.method ?? "get")
    )
      throw error;
    const bearer = config.headers.Authorization;
    const rejectedAccess =
      typeof bearer === "string" && bearer.startsWith("Bearer ")
        ? bearer.slice(7)
        : null;
    const refresh = refreshToken();
    if (!rejectedAccess || !refresh) throw error;
    try {
      await renewAccess(rejectedAccess, refresh);
    } catch (reason) {
      // Keep the original identity on refusal. Never silently replace a player or
      // administrator with a guest, and retain credentials on a network failure.
      if (
        axios.isAxiosError(reason) &&
        [401, 403].includes(reason.response?.status ?? 0)
      )
        throw error;
      throw reason;
    }
    config.sessionRetried = true;
    return http.request(config);
  },
);

export function isSessionExpired(error: unknown): boolean {
  return axios.isAxiosError(error) && error.response?.status === 401;
}

/** Translate transport/auth failures; preserve domain error codes for game actions. */
export function errorCode(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const body = error.response?.data as
      { detail?: unknown; message?: unknown } | undefined;
    const message =
      typeof body?.detail === "string"
        ? body.detail
        : typeof body?.message === "string"
          ? body.message
          : null;
    if (error.response?.status === 401 && !isAuthRequest(error.config?.url)) {
      return "登录状态已失效，请重新登录。";
    }
    if (message) return message;
    if (!error.response) return "暂时无法连接服务，请检查连接后重试。";
    return "操作暂时未完成，请稍后重试。";
  }
  return error instanceof Error
    ? error.message
    : "操作暂时未完成，请稍后重试。";
}
