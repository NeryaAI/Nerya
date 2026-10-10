"use client";

import type { AssistantProfile } from "../lib/assistants";
import { roles } from "../lib/roleAvatars";
import { RoleAvatar } from "./RoleAvatar";

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

/** Empty avatars and the original main assistant's "N" use the mascot. */
export function AssistantAvatar({profile, size = 30, className = ""}: {
  profile?: Pick<AssistantProfile, "id" | "name" | "avatar"> | null;
  size?: number;
  className?: string;
}) {
  const avatar = profile?.avatar?.trim();
  if (!avatar || (profile?.id === "main" && avatar === "N")) {
    return <NeryaAvatar size={size} className={`shrink-0 ${className}`} alt="" />;
  }
  if (avatar.startsWith("role:") && (roles as readonly string[]).includes(avatar.slice(5))) {
    return <RoleAvatar role={avatar.slice(5)} size={size} className={className} alt="" />;
  }
  if (avatar.startsWith("data:image/png;base64,")) {
    return <img src={avatar} alt="" width={size} height={size} draggable={false}
      className={`shrink-0 rounded-full object-cover ${className}`} style={{width: size, height: size}} />;
  }
  return <span aria-hidden="true" className={`inline-flex shrink-0 items-center justify-center rounded-full bg-brand-500/10 ${className}`}
    style={{width: size, height: size, fontSize: size * 0.45}}>{avatar}</span>;
}
