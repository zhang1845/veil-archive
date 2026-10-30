import Link from "next/link";

export function Brand({ inverse = false }: { inverse?: boolean }) {
  return (
    <Link
      href="/"
      aria-label="雾幕档案首页"
      className={`brand ${inverse ? "brand-inverse" : ""}`}
    >
      <span className="brand-mark" aria-hidden="true">
        雾
      </span>
      <span className="brand-wordmark">
        <span>雾幕档案</span>
        <small>THE VEIL ARCHIVE</small>
      </span>
    </Link>
  );
}
