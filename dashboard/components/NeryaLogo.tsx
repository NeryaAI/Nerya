"use client";

/**
 * 统一的 Nerya 品牌 logo 组件。
 *
 * 使用 branding 里的高清 PNG（512x512）作为源，浏览器按需缩放。
 * 所有 sidebar / topheader / landing 的 N 字 logo 都经过这里。
 */
export function NeryaLogo({
  size = 24,
  className = "",
  alt = "Nerya",
}: {
  size?: number;
  className?: string;
  alt?: string;
}) {
  return (
    <img
      src="/branding/svg/logo-512x512.png"
      alt={alt}
      width={size}
      height={size}
      className={className}
      style={{ display: "block" }}
      draggable={false}
    />
  );
}
