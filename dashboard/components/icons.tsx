"use client";

import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function base({ size = 18, ...rest }: IconProps) {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    ...rest,
  };
}

export function OverviewIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <rect x="3" y="3" width="8" height="8" rx="1.5" />
      <rect x="13" y="3" width="8" height="5" rx="1.5" />
      <rect x="13" y="10" width="8" height="11" rx="1.5" />
      <rect x="3" y="13" width="8" height="8" rx="1.5" />
    </svg>
  );
}

export function AgentsIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <circle cx="12" cy="8" r="3" />
      <path d="M5 20c0-3.9 3.1-7 7-7s7 3.1 7 7" />
      <path d="M12 2v1.5" />
      <path d="M8.5 3.5l.8 1.3" />
      <path d="M15.5 3.5l-.8 1.3" />
    </svg>
  );
}

export function SubagentsIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <circle cx="12" cy="5" r="2" />
      <circle cx="5" cy="19" r="2" />
      <circle cx="19" cy="19" r="2" />
      <path d="M12 7v4M12 11l-5.5 6M12 11l5.5 6" />
    </svg>
  );
}

export function SkillsIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 2l2.5 4.9L20 8l-4 3.8.9 5.4L12 14.8 7.1 17.2 8 11.8 4 8l5.5-1.1L12 2z" />
    </svg>
  );
}

export function TriggersIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M13 3L5 13h6l-1 8 8-10h-6l1-8z" />
    </svg>
  );
}

export function ScriptsIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M6 3h10l4 4v14H6z" />
      <path d="M16 3v4h4" />
      <path d="M9 12l-2 2 2 2" />
      <path d="M13 12l2 2-2 2" />
    </svg>
  );
}

export function PortfolioIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <rect x="3" y="6" width="18" height="13" rx="2" />
      <path d="M9 6V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V6" />
      <path d="M3 11h18" />
      <path d="M11 14h2" />
    </svg>
  );
}

export function OrdersIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M3 17l4-4 3 3 5-6 6 6" />
      <path d="M3 21h18" />
      <circle cx="7" cy="13" r="1.2" />
      <circle cx="10" cy="16" r="1.2" />
      <circle cx="15" cy="10" r="1.2" />
      <circle cx="21" cy="16" r="1.2" />
    </svg>
  );
}

export function StrategiesIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M4 19V5" />
      <path d="M4 19h16" />
      <path d="M7 15l4-6 3 3 5-8" />
      <circle cx="18" cy="4" r="1.2" />
    </svg>
  );
}

export function HistoryIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M3 12a9 9 0 1 0 3-6.7" />
      <path d="M3 4v5h5" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}

export function MessagesIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M21 15a3 3 0 0 1-3 3H8l-5 3V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3z" />
      <path d="M8 10h8" />
      <path d="M8 13h5" />
    </svg>
  );
}

export function MemoryIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 3c4.5 0 8 2.5 8 6 0 1.4-.6 2.6-1.6 3.6.6.8.9 1.7.9 2.7 0 2.6-2.8 4.7-6.3 4.7s-6.3-2.1-6.3-4.7c0-1 .3-1.9.9-2.7A5.3 5.3 0 0 1 4 9c0-3.5 3.5-6 8-6z" />
      <path d="M9 10h.01M15 10h.01M12 14c1 0 2-.3 2.5-1" />
    </svg>
  );
}

export function EvolutionIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M4 20c6-8 10-8 16 0" />
      <circle cx="4" cy="20" r="1.6" />
      <circle cx="20" cy="20" r="1.6" />
      <path d="M12 14V4" />
      <path d="M8 8l4-4 4 4" />
    </svg>
  );
}

export function SecurityIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 3l8 3v6c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V6l8-3z" />
      <path d="M9 12l2 2 4-4" />
    </svg>
  );
}

export function NeryaMark(props: IconProps) {
  // "Evolutionary Brain N" — the N letterform is built from three
  // excitable axons that branch into dendrites (the brain) and a
  // glowing pulsing core node at the synapse (the seat of evolution).
  // Three small data packets cascade along the diagonal axon to
  // suggest learning / adaptation in motion.
  return (
    <svg {...base(props)} viewBox="0 0 32 32" strokeWidth={0} fill="none">
      <defs>
        <linearGradient id="nmg" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#b48bff" />
          <stop offset="55%" stopColor="#8b5cf6" />
          <stop offset="100%" stopColor="#22d3ee" />
        </linearGradient>
        <radialGradient id="nmn" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#ffffff" stopOpacity={0.95} />
          <stop offset="100%" stopColor="#b48bff" stopOpacity={0.25} />
        </radialGradient>
        <radialGradient id="nmcore" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#ffffff" stopOpacity={1} />
          <stop offset="55%" stopColor="#c9a8ff" stopOpacity={0.85} />
          <stop offset="100%" stopColor="#22d3ee" stopOpacity={0} />
        </radialGradient>
      </defs>
      {/* primary axons forming the N */}
      <g stroke="url(#nmg)" strokeWidth={2.4} strokeLinecap="round" fill="none">
        <path d="M7 25 L7 7" />
        <path d="M7 7 Q11.5 13 16 16 T25 25" />
        <path d="M25 7 L25 25" />
      </g>
      {/* dendrites — short branches off the verticals to read as a brain */}
      <g stroke="url(#nmg)" strokeWidth={1} strokeLinecap="round" fill="none" opacity={0.55}>
        <path d="M7 12 L4 11" />
        <path d="M7 18 L4 19" />
        <path d="M25 12 L28 11" />
        <path d="M25 18 L28 19" />
        <path d="M16 16 L13 19" />
        <path d="M16 16 L19 13" />
      </g>
      {/* glowing pulsing core at the synapse */}
      <circle cx="16" cy="16" r="4.2" fill="url(#nmcore)" opacity={0.55}>
        <animate attributeName="r" values="3.8;4.6;3.8" dur="3.2s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0.45;0.7;0.45" dur="3.2s" repeatCount="indefinite" />
      </circle>
      {/* terminal + relay nodes */}
      <g fill="url(#nmn)">
        <circle cx="7" cy="7" r="1.6" />
        <circle cx="16" cy="16" r="2" />
        <circle cx="25" cy="25" r="1.6" />
        <circle cx="7" cy="25" r="1.1" />
        <circle cx="25" cy="7" r="1.1" />
      </g>
      {/* data packets cascading along the diagonal axon */}
      <g fill="#22d3ee">
        <circle cx="11" cy="11" r="0.5" opacity="0.8" />
        <circle cx="13.5" cy="13.5" r="0.4" opacity="0.65" />
        <circle cx="19" cy="19" r="0.4" opacity="0.65" />
        <circle cx="21.5" cy="21.5" r="0.5" opacity="0.8" />
      </g>
    </svg>
  );
}

export function ChatIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M21 15a3 3 0 0 1-3 3H8l-5 3V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3z" />
      <circle cx="9" cy="11" r="1" fill="currentColor" />
      <circle cx="13" cy="11" r="1" fill="currentColor" />
      <circle cx="17" cy="11" r="1" fill="currentColor" />
    </svg>
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06A2 2 0 1 1 4.29 16.96l.06-.06A1.65 1.65 0 0 0 4.68 15 1.65 1.65 0 0 0 3.17 14H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </svg>
  );
}

export function BellIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10 21a2 2 0 0 0 4 0" />
    </svg>
  );
}

export function StarIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 2l2.9 6.3 6.9.6-5.2 4.7 1.6 6.8L12 17l-6.2 3.4 1.6-6.8L2.2 8.9l6.9-.6z" />
    </svg>
  );
}

export function MoonIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M21 12.5A9 9 0 1 1 11.5 3a7 7 0 0 0 9.5 9.5z" />
    </svg>
  );
}

export function ChevronLeftIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M15 18l-6-6 6-6" />
    </svg>
  );
}

export function ChevronRightIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M9 6l6 6-6 6" />
    </svg>
  );
}

export function ChevronDownIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M6 9l6 6 6-6" />
    </svg>
  );
}

export function PlusIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

export function SparkIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 3v3M12 18v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M3 12h3M18 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" />
    </svg>
  );
}

export function SendIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M22 2L11 13" />
      <path d="M22 2l-7 20-4-9-9-4z" />
    </svg>
  );
}

export function StopIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  );
}

export function CopyIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

export function EditIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
    </svg>
  );
}

export function TrashIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M19 6l-1 15H6L5 6" />
      <path d="M10 11v6M14 11v6" />
    </svg>
  );
}

export function CheckIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M20 6L9 17l-5-5" />
    </svg>
  );
}

export function XIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M18 6L6 18M6 6l12 12" />
    </svg>
  );
}

export function ShieldCheckIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 3l8 3v6c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V6l8-3z" />
      <path d="M9 12l2 2 4-4" />
    </svg>
  );
}

export function ShieldXIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 3l8 3v6c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V6l8-3z" />
      <path d="M9.5 10.5l5 5M14.5 10.5l-5 5" />
    </svg>
  );
}

export function WrenchIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.4 2.4-3-3 2.4-2.4z" />
    </svg>
  );
}

export function ScriptRunIcon(props: IconProps) {
  return (
    <svg {...base(props)}>
      <polygon points="5 3 19 12 5 21 5 3" />
    </svg>
  );
}

export const NAV_ICONS: Record<string, (props: IconProps) => JSX.Element> = {
  "/dashboard": OverviewIcon,
  "/chat": ChatIcon,
  "/portfolio": PortfolioIcon,
  "/accounts": SecurityIcon,
  "/orders": HistoryIcon,
  "/incidents": BellIcon,
  "/strategies": StrategiesIcon,
  "/agents": AgentsIcon,
  "/skills": SkillsIcon,
  "/workflows": TriggersIcon,
  "/inbox": BellIcon,
  "/tasks": AgentsIcon,
  "/self-evolution": EvolutionIcon,
  "/settings": SettingsIcon,
};

/**
 * Backend-emitted icon name → React component.
 *
 * ``routes_operator.py`` returns short, semantic icon hints (``home``,
 * ``inbox``, ``portfolio`` …). The Sidebar resolves them through this
 * map so the navigation shape stays driven by the backend without
 * pinning the dashboard to specific Heroicons.
 */
export const NAV_ICON_BY_NAME: Record<string, (props: IconProps) => JSX.Element> = {
  home: OverviewIcon,
  chat: ChatIcon,
  portfolio: PortfolioIcon,
  accounts: SecurityIcon,
  orders: HistoryIcon,
  incidents: BellIcon,
  strategy: StrategiesIcon,
  workflow: TriggersIcon,
  inbox: BellIcon,
  settings: SettingsIcon,
  agents: AgentsIcon,
  subagents: SubagentsIcon,
  skills: SkillsIcon,
  scripts: ScriptsIcon,
  history: HistoryIcon,
  messages: MessagesIcon,
  memory: MemoryIcon,
  evolution: EvolutionIcon,
  security: SecurityIcon,
};
