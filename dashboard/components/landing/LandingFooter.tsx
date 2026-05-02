"use client";

import Link from "next/link";
import { motion } from "framer-motion";

export function LandingFooter() {
  return (
    <footer className="relative px-6 lg:px-10 pt-16 pb-10 border-t border-brand-500/10 mt-10">
      <div className="mx-auto max-w-6xl">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5 }}
          className="glass rounded-2xl px-8 py-12 text-center overflow-hidden relative"
        >
          {/* 背景光晕 */}
          <div
            className="absolute inset-0 pointer-events-none"
            style={{
              background:
                "radial-gradient(60% 80% at 50% 100%, rgba(139,92,246,0.18), transparent 70%)",
            }}
          />
          <div className="relative z-10">
            <div className="inline-flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.28em] text-fluid-300 mb-4">
              <span className="w-1 h-1 rounded-full bg-fluid-400 shadow-[0_0_8px_rgba(34,211,238,0.8)]" />
              <span>Ready to run</span>
            </div>
            <h3 className="text-[28px] md:text-[38px] font-semibold tracking-tight text-gradient-brand leading-[1.15]">
              Your next edge is an evolution away.
            </h3>
            <p className="mt-4 text-ink-400 text-[14px] max-w-xl mx-auto leading-relaxed">
              Paper mode is free, live trades stay under a risk gate. Bring your
              own models or use the default fleet.
            </p>
            <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
              <Link
                href="/dashboard"
                className="group inline-flex items-center gap-2 rounded-xl px-6 py-3 text-sm font-semibold text-white transition-all"
                style={{
                  background:
                    "linear-gradient(135deg, rgba(139,92,246,0.9), rgba(124,58,237,0.9))",
                  boxShadow:
                    "0 0 0 1px rgba(139,92,246,0.4), 0 8px 32px -8px rgba(139,92,246,0.6)",
                }}
              >
                <span>Launch Console</span>
                <svg
                  className="transition-transform group-hover:translate-x-1"
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                >
                  <path d="M5 12h14m-6-7l7 7-7 7" />
                </svg>
              </Link>
              <a
                href="https://github.com/veithly/Nerya"
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-xl border border-white/15 bg-white/[0.03] backdrop-blur-md px-6 py-3 text-sm font-semibold text-ink-100 hover:bg-white/[0.06] hover:border-white/25 transition-all"
              >
                <span>View on GitHub</span>
              </a>
            </div>
          </div>
        </motion.div>

        {/* 底部小字 */}
        <div className="mt-10 flex flex-wrap items-center justify-between gap-4 text-[11px] text-ink-500">
          <div className="flex items-center gap-2 font-mono tracking-wide">
            <span className="w-1 h-1 rounded-full bg-accent-400" />
            <span>NERYA · evolutionary brain</span>
            <span className="opacity-50">·</span>
            <span>v0.1.0</span>
          </div>
          <div className="flex items-center gap-5 font-mono">
            <a
              href="https://github.com/veithly/Nerya"
              target="_blank"
              rel="noreferrer"
              className="hover:text-ink-200 transition-colors"
            >
              GitHub
            </a>
            <Link href="/dashboard" className="hover:text-ink-200 transition-colors">
              Console
            </Link>
            <Link href="/chat" className="hover:text-ink-200 transition-colors">
              Chat
            </Link>
          </div>
        </div>
      </div>
    </footer>
  );
}
