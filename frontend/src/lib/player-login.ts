/** Limit the post-login destination to the existing player routes. */
export function playerReturnPath(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\") || value.length > 2048) {
    return "/scripts";
  }
  try {
    const target = new URL(value, "https://player.invalid");
    if (target.origin !== "https://player.invalid" || !["/scripts", "/records", "/play"].includes(target.pathname)) {
      return "/scripts";
    }
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return "/scripts";
  }
}
