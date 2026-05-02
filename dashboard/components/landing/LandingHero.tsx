"use client";

import Link from "next/link";
import { motion, useScroll, useTransform } from "framer-motion";
import { useRef } from "react";
import { LandingStage } from "./LandingStage";
import { FloatingTickers } from "./FloatingTickers";
import { Typewriter } from "./Typewriter";
import { NeryaLogo } from "../NeryaLogo";

export function LandingHero() {
  const ref = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ["start start", "end start"],
  });

  // Stage 的 scroll-linked 变换：缩放 + 上移 + 淡出
  const stageScale = useTransform(scrollYProgress, [0, 0.9], [1, 0.85]);
  const stageY = useTransform(scrollYProgress, [0, 1], [0, -80]);
  const stageOpacity = useTransform(scrollYProgress, [0, 0.85], [1, 0]);

  // 标题的 scroll fade + 上移
  const titleY = useTransform(scrollYProgress, [0, 1], [0, -80]);
  const titleOpacity = useTransform(scrollYProgress, [0, 0.6], [1, 0]);

  // 描述 + CTA 的 fade
  const subY = useTransform(scrollYProgress, [0, 1], [0, -60]);
  const subOpacity = useTransform(scrollYProgress, [0, 0.5], [1, 0]);

  // 背景光晕的反向视差
  const bgShift = useTransform(scrollYProgress, [0, 1], [0, 80]);
  const tickerShift = useTransform(scrollYProgress, [0, 1], [0, -180]);

  return (
    <section
      ref={ref}
      className="relative min-h-screen w-full overflow-hidden"
    >
      {/* 背景网格 */}
      <div className="absolute inset-0 grid-bg opacity-20 pointer-events-none" />

      {/* 顶部光晕（反向视差） */}
      <motion.div
        className="absolute inset-0 pointer-events-none"
        style={{
          background:
            "radial-gradient(60% 50% at 50% 0%, rgba(139,92,246,0.18), transparent 70%)",
          y: bgShift,
        }}
      />

      {/* 浮动行情 ticker（加速视差） */}
      <motion.div
        className="absolute inset-0 pointer-events-none"
        style={{ y: tickerShift }}
      >
        <FloatingTickers />
      </motion.div>

      {/* 顶部 nav */}
      <nav className="relative z-20 flex items-center justify-between px-6 lg:px-10 py-5">
        <div className="flex items-center gap-3">
          <div className="relative w-9 h-9 rounded-xl overflow-hidden ring-1 ring-brand-500/40 flex items-center justify-center shadow-glow bg-black/30">
            <NeryaLogo size={36} />
            <span className="absolute -inset-px rounded-xl ring-1 ring-white/10 pointer-events-none" />
          </div>
          <div className="leading-none">
            <div className="text-white text-[14px] font-semibold tracking-[0.26em]">
              NERYA
            </div>
            <div className="mt-0.5 text-[8px] uppercase tracking-[0.32em] text-fluid-400/80 font-mono">
              evolutionary brain · v0.1.0
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <a
            href="https://github.com/veithly/Nerya"
            target="_blank"
            rel="noreferrer"
            className="text-[12px] font-mono text-ink-400 hover:text-ink-100 transition-colors tracking-wide"
          >
            GitHub
          </a>
          <Link
            href="/dashboard"
            className="group relative inline-flex items-center gap-2 rounded-lg border border-brand-500/40 bg-brand-500/10 backdrop-blur-md px-4 py-2 text-[12px] font-semibold text-white tracking-wide hover:bg-brand-500/20 hover:border-brand-400/60 transition-all overflow-hidden"
          >
            <span className="relative z-10">Enter Console</span>
            <svg
              className="relative z-10 transition-transform group-hover:translate-x-1"
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <path d="M5 12h14m-6-7l7 7-7 7" />
            </svg>
            <span
              className="absolute inset-0 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none"
              style={{
                background:
                  "linear-gradient(120deg, transparent 30%, rgba(139,92,246,0.3) 50%, transparent 70%)",
                backgroundSize: "200% 100%",
                animation: "shimmer 1.8s linear infinite",
              }}
            />
          </Link>
        </div>
      </nav>

      {/* 主视觉 · 单列居中 */}
      <div className="relative z-10 px-6 lg:px-10 pt-10 lg:pt-16 pb-10">
        <div className="mx-auto max-w-6xl">
          {/* 顶部文字区 · 居中对齐 */}
          <motion.div
            style={{ y: titleY, opacity: titleOpacity }}
            className="text-center"
          >
            {/* Eyebrow */}
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5 }}
              className="inline-flex items-center gap-2 rounded-full border border-brand-500/30 bg-brand-500/5 backdrop-blur-md px-3 py-1 mb-6"
            >
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-accent-400" />
              </span>
              <span className="text-[10px] font-mono uppercase tracking-[0.28em] text-brand-200">
                live · self-evolving runtime
              </span>
            </motion.div>

            {/* 主标题 */}
            <motion.h1
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6, delay: 0.1 }}
              className="text-[44px] md:text-[64px] lg:text-[78px] leading-[1.02] font-bold tracking-tight text-gradient-brand max-w-5xl mx-auto"
            >
              Agents that evolve
              <br />
              <span className="inline-block relative">
                to survive the market
                <span
                  className="absolute -inset-x-2 -bottom-1 h-[2px] opacity-50"
                  style={{
                    background:
                      "linear-gradient(90deg, transparent, #b48bff, #22d3ee, transparent)",
                  }}
                />
              </span>
              <span className="text-ink-400">.</span>
            </motion.h1>
          </motion.div>

          {/* 副标题 */}
          <motion.p
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.2 }}
            style={{ y: subY, opacity: subOpacity }}
            className="mt-6 mx-auto text-[14px] md:text-[16px] text-ink-300 max-w-2xl leading-relaxed text-center min-h-[72px]"
          >
            <Typewriter
              lines={[
                "Nerya is a trading-native, self-evolving autonomous agent runtime. Skills compose, strategies learn, the kernel rewrites itself — safely — against live markets.",
              ]}
              speed={14}
              cursorClassName="text-brand-300"
            />
          </motion.p>

          {/* CTA 按钮 */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.4 }}
            style={{ y: subY, opacity: subOpacity }}
            className="mt-8 flex flex-wrap items-center justify-center gap-3"
          >
            <Link
              href="/dashboard"
              className="group relative inline-flex items-center gap-2 rounded-full px-6 py-3 text-sm font-semibold text-white transition-all"
              style={{
                background:
                  "linear-gradient(135deg, rgba(139,92,246,0.95), rgba(124,58,237,0.95))",
                boxShadow:
                  "0 0 0 1px rgba(139,92,246,0.4), 0 8px 32px -4px rgba(139,92,246,0.6)",
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
            <Link
              href="/chat"
              className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/[0.03] backdrop-blur-md px-6 py-3 text-sm font-semibold text-ink-100 hover:bg-white/[0.08] hover:border-white/25 transition-all"
            >
              <span>Try the Agent</span>
            </Link>
          </motion.div>

          {/* 中央舞台 */}
          <motion.div
            style={{ scale: stageScale, y: stageY, opacity: stageOpacity }}
            className="mt-14 lg:mt-20"
          >
            <LandingStage />
          </motion.div>

          {/* 滚动提示 */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.6, delay: 1.5 }}
            style={{ opacity: subOpacity }}
            className="mt-10 text-center"
          >
            <motion.span
              animate={{ y: [0, 6, 0] }}
              transition={{ duration: 1.8, repeat: Infinity, ease: "easeInOut" }}
              className="inline-block text-[10px] font-mono text-ink-500 uppercase tracking-[0.32em]"
            >
              ▾ scroll
            </motion.span>
          </motion.div>
        </div>
      </div>
    </section>
  );
}
