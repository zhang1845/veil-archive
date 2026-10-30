export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8010";

export const SITE_BASE_PATH = process.env.NEXT_PUBLIC_SITE_BASE_PATH || "";

export const siteAsset = (path: string) => `${SITE_BASE_PATH}${path}`;
