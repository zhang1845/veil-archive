import type { NextConfig } from "next";

const cloudbaseStaticExport = process.env.CLOUDBASE_STATIC_EXPORT === "true";
const siteBasePath = cloudbaseStaticExport
  ? process.env.CLOUDBASE_SITE_BASE_PATH || "/veil-archive"
  : "";
if (siteBasePath && !/^\/[a-z0-9][a-z0-9-]*(?:\/[a-z0-9][a-z0-9-]*)*$/.test(siteBasePath)) {
  throw new Error("Invalid CLOUDBASE_SITE_BASE_PATH");
}
if (cloudbaseStaticExport) {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL;
  let valid = false;
  try {
    const parsed = new URL(apiUrl ?? "");
    valid = parsed.protocol === "https:" && !parsed.username && !parsed.password &&
      !parsed.search && !parsed.hash;
  } catch {
    // A deployed static bundle must never silently use the local dev backend.
  }
  if (!valid) {
    throw new Error("CloudBase static export requires an HTTPS NEXT_PUBLIC_API_URL");
  }
}

const nextConfig: NextConfig = {
  reactStrictMode: true,
  basePath: siteBasePath,
  env: { NEXT_PUBLIC_SITE_BASE_PATH: siteBasePath },
  output: cloudbaseStaticExport ? "export" : "standalone",
  ...(cloudbaseStaticExport
    ? { trailingSlash: true, images: { unoptimized: true } }
    : {}),
};

export default nextConfig;
