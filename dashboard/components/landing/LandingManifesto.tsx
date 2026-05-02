"use client";

import { ScrollTextReveal } from "./ScrollTextReveal";

/**
 * 介于 Features 和 Architecture 之间的"宣言"段。
 * 用滚动逐行浮现，读起来像朗诵，适合路演截图。
 */
export function LandingManifesto() {
  return (
    <section className="relative py-32 md:py-40 px-6 lg:px-10">
      {/* 两侧竖向光柱装饰 */}
      <div
        className="absolute left-0 top-10 bottom-10 w-px pointer-events-none"
        style={{
          background:
            "linear-gradient(180deg, transparent, rgba(139,92,246,0.4), transparent)",
        }}
      />
      <div
        className="absolute right-0 top-10 bottom-10 w-px pointer-events-none"
        style={{
          background:
            "linear-gradient(180deg, transparent, rgba(34,211,238,0.4), transparent)",
        }}
      />

      <div className="mx-auto max-w-3xl">
        <div className="inline-flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.28em] text-brand-300 mb-8">
          <span className="w-1 h-1 rounded-full bg-brand-400 shadow-[0_0_8px_rgba(139,92,246,0.8)]" />
          <span>Manifesto</span>
        </div>

        <ScrollTextReveal
          className="space-y-6 text-[28px] md:text-[44px] font-semibold tracking-tight leading-[1.15]"
          lines={[
            <span key="1" className="text-white">
              Don't trade the market.
            </span>,
            <span key="2" className="text-ink-300">
              Let the market
              <span className="text-fluid-300"> teach </span>
              the agent.
            </span>,
            <span key="3" className="text-ink-400">
              Every observation is a{" "}
              <span className="text-gradient-brand">lesson</span>.
            </span>,
            <span key="4" className="text-ink-400">
              Every lesson{" "}
              <span className="text-brand-300">rewrites</span> the kernel.
            </span>,
            <span key="5" className="text-ink-400">
              Every rewrite makes the next trade
              <em className="not-italic text-accent-400"> smarter</em>.
            </span>,
          ]}
        />
      </div>
    </section>
  );
}
