/** 生成符合后端幂等键格式（^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$）的唯一键。 */
export function newKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `k${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
