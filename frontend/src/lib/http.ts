import axios, { type InternalAxiosRequestConfig } from "axios";
import { API_BASE_URL } from "./config";
import { accessToken, refreshToken, saveToken } from "./tokens";
import type { Token } from "@/types/api";

export const http = axios.create({ baseURL: API_BASE_URL });

const serviceMessages: Record<string, string> = {
  CLOUDBASE_MODEL_DISABLED: "尚未配置创作模型，暂时无法生成或审核剧本。",
  CLOUDBASE_MODELS_DISABLED: "尚未配置创作模型，暂时无法生成或审核剧本。",
  PAID_EXECUTION_DISABLED: "当前未启用模型调用，任务尚未开始。",
  CLOUDBASE_ADMIN_REQUIRED: "请使用管理员账号进入剧本工作台。",
  CLOUDBASE_SOURCE_STORAGE_NOT_CONFIGURED: "私有来源存储尚未配置，暂时无法核验。",
  CLOUDBASE_SOURCE_NOT_REGISTERED: "这份候选尚未登记来源，请先准备并绑定来源文件。",
  CLOUDBASE_SOURCE_INVALID: "来源核验未通过，请检查文件与候选的对应关系。",
  PRIVATE_STORAGE_INTEGRITY_FAILED: "来源文件校验未通过，本次操作未保存。",
  CLOUDBASE_ADMIN_WRITE_CONFLICT: "内容已被其他操作更新，请刷新后重试。",
  VERSION_CONFLICT: "内容已被其他操作更新，请刷新后重试。",
  CLOUDBASE_ADMIN_SAVE_LIMIT: "当前工作区已达到保存限制，本次操作未保存。",
  CLOUDBASE_ADMIN_RECEIPT_TOO_LARGE: "本次审核记录内容过长，请缩短说明后重新提交。",
  PUBLICATION_GATE_BLOCKED: "审核条件尚未全部满足，请查看发布检查项。",
  PUBLICATION_BASIS_CHANGED: "审核依据已有变化，请重新核对并确认。",
  PUBLICATION_APPROVAL_EXPIRED: "原发布确认已失效，请重新核对审核依据。",
  CLOUDBASE_CREATIVE_JOB_INCOMPATIBLE: "任务记录与当前工作台不兼容，需要管理员核对。",
  CLOUDBASE_JOB_RESPONSE_INVALID: "任务记录暂时无法读取，请稍后重试。",
  JOB_CANCEL_ADMIN_REQUIRED: "请使用有效的管理员账号取消任务。",
  JOB_CANCEL_LEASE_ACTIVE: "任务正在执行，暂时不能取消。请稍后刷新状态。",
  JOB_CANCEL_RESULT_UNKNOWN: "已有调用结果待核对，暂不能取消或重复执行。",
  JOB_CANCEL_USAGE_PENDING: "任务仍有待核对的额度记录，暂时不能取消。",
  JOB_CANCEL_STATE_CONFLICT: "当前任务状态不支持取消，请刷新后核对。",
  JOB_CANCEL_REVISION_CONFLICT: "任务进度已经变化，请刷新后再取消。",
  JOB_CANCEL_IDEMPOTENCY_CONFLICT: "这次取消请求与原记录不一致，请刷新状态。",
  GATEWAY_UNAVAILABLE: "服务暂时不可用，请稍后重试。",
  GATEWAY_FORBIDDEN: "服务暂时无法访问数据，请联系管理员检查配置。",
};

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
    if (message) return serviceMessages[message] ?? message;
    if (!error.response) return "暂时无法连接服务，请检查连接后重试。";
    return "操作暂时未完成，请稍后重试。";
  }
  return error instanceof Error
    ? error.message
    : "操作暂时未完成，请稍后重试。";
}
