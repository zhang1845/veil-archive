import axios from "axios";
import { http } from "@/lib/http";
import type { Token } from "@/types/api";
import { accessToken, saveToken } from "@/lib/tokens";
export { saveToken, clearToken } from "@/lib/tokens";

export async function anonymousLogin(): Promise<Token> {
  const { data } = await http.post<Token>("/api/auth/anonymous-login");
  return data;
}

export function hasToken(): boolean {
  return !!accessToken();
}

let guestLogin: Promise<void> | null = null;
/** Preserve an existing player/admin identity; concurrent mounts share one guest login. */
export async function ensureSession(): Promise<void> {
  if (hasToken()) return;
  if (!guestLogin)
    guestLogin = anonymousLogin()
      .then(saveToken)
      .finally(() => {
        guestLogin = null;
      });
  await guestLogin;
}

export async function passwordLogin(
  username: string,
  password: string,
  requireAdmin = false,
): Promise<void> {
  const { data } = await http.post<Token>("/api/auth/login", {
    username,
    password,
  });
  if (
    requireAdmin &&
    (data.user.is_admin !== true || data.user.is_active !== true)
  ) {
    throw new Error("请使用有效的管理员账号登录工作台");
  }
  saveToken(data);
}

/** Check the server's current identity before mounting any admin content. */
export async function hasAdminSession(): Promise<boolean> {
  if (!hasToken()) return false;
  try {
    const { data } = await http.get<{ is_admin: boolean; is_active: boolean }>(
      "/api/auth/me",
    );
    return data.is_admin === true && data.is_active === true;
  } catch (error) {
    if (
      axios.isAxiosError(error) &&
      [401, 403].includes(error.response?.status ?? 0)
    ) {
      return false;
    }
    throw error;
  }
}
