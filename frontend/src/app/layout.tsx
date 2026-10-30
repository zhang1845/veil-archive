import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "雾幕档案 · 单人互动推理", template: "%s · 雾幕档案" },
  applicationName: "雾幕档案",
  description:
    "翻开案卷，成为故事中的一员。在雾幕档案，与 AI 角色交谈、搜集线索，亲手揭开真相。",
};

export const viewport: Viewport = { themeColor: "#0c1114" };

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
