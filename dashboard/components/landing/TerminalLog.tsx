"use client";

import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useState } from "react";

const LOG_LINES = [
  { prefix: ">", text: "booting evolutionary kernel...", tone: "muted" },
  { prefix: ">", text: "loading 12 skills, 3 subagents", tone: "muted" },
  { prefix: "[mkt]", text: "streaming BTCUSDT @ 1m", tone: "fluid" },
  { prefix: "[agent]", text: "route=portfolio_manager tier=high", tone: "brand" },
  { prefix: "[risk]", text: "drawdown check passed (0.4%)", tone: "ok" },
  { prefix: "[evo]", text: "proposal: scale_in_threshold 0.35 → 0.42", tone: "brand" },
  { prefix: "[trade]", text: "LONG BTCUSDT 0.5x @ 68,420", tone: "ok" },
  { prefix: "[evo]", text: "strategy v1.3.2 → v1.3.3  ✓", tone: "ok" },
] as const;

const TONE_STYLES: Record<string, string> = {
  muted: "text-ink-500",
  fluid: "text-fluid-300",
  brand: "text-brand-200",
  ok: "text-accent-400",
};

export function TerminalLog() {
  const [visible, setVisible] = useState<number>(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setVisible((v) => (v + 1) % (LOG_LINES.length + 2));
    }, 900);
    return () => clearInterval(interval);
  }, []);

  const shown = LOG_LINES.slice(0, Math.min(visible, LOG_LINES.length));

  return (
    <div className="font-mono text-[11px] leading-[1.9] min-h-[170px] w-full max-w-md">
      <AnimatePresence mode="popLayout">
        {shown.map((line, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, x: -8 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.25 }}
            className="flex items-center gap-2"
          >
            <span className={`${TONE_STYLES[line.tone]} shrink-0`}>{line.prefix}</span>
            <span className="text-ink-300 truncate">{line.text}</span>
            {i === shown.length - 1 && visible <= LOG_LINES.length ? (
              <motion.span
                className="inline-block w-1.5 h-3 bg-brand-300"
                animate={{ opacity: [1, 0, 1] }}
                transition={{ duration: 1, repeat: Infinity }}
              />
            ) : null}
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
