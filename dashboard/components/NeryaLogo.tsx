"use client";

/**
 * Unified Nerya brand-logo component.
 *
 * Sources the high-res ``Logo.png`` shipped under
 * ``public/branding/`` (the new "Nerya" branded mark replacing the
 * legacy 512x512 SVG render). The browser scales it on demand, so the
 * one source covers every consumer (top nav, chat header, landing
 * splash, favicon-adjacent surfaces).
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
      src="/branding/Logo.png"
      alt={alt}
      width={size}
      height={size}
      className={className}
      style={{ display: "block", objectFit: "contain" }}
      draggable={false}
    />
  );
}

/**
 * Nerya assistant avatar — the smiling-character reference asset
 * (``Nerya.png``) used for the chat / agent presence circle. Distinct
 * from ``NeryaLogo`` (the brand mark) so the two assets can evolve
 * independently and we can swap one without touching the other.
 */
export function NeryaAvatar({
  size = 36,
  className = "",
  alt = "Nerya assistant",
}: {
  size?: number;
  className?: string;
  alt?: string;
}) {
  return (
    <img
      src="/branding/Nerya.png"
      alt={alt}
      width={size}
      height={size}
      className={className}
      style={{ display: "block", objectFit: "cover", borderRadius: "999px" }}
      draggable={false}
    />
  );
}
