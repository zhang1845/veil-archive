import type { Token } from "@/types/api";

const ACCESS_KEY = "access_token";
const REFRESH_KEY = "refresh_token";

export function accessToken(): string | null {
  return typeof window === "undefined"
    ? null
    : localStorage.getItem(ACCESS_KEY);
}

export function refreshToken(): string | null {
  return typeof window === "undefined"
    ? null
    : localStorage.getItem(REFRESH_KEY);
}

export function saveToken(
  token: Pick<Token, "access_token" | "refresh_token">,
) {
  localStorage.setItem(ACCESS_KEY, token.access_token);
  if (token.refresh_token)
    localStorage.setItem(REFRESH_KEY, token.refresh_token);
  else localStorage.removeItem(REFRESH_KEY);
}

export function clearToken() {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}
