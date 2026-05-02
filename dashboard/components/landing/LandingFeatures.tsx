"use client";

import { motion, useMotionValue, useSpring, useTransform } from "framer-motion";
import { useRef, type MouseEvent, type ReactNode } from "react";

const FEATURES = [
  {
    icon: "🧠",
    badge: "Evolution loop",
    title: "Self-evolving",
    body:
      "Every turn produces evidence. The kernel proposes strategy deltas, runs them through risk + backtest gates, and promotes winners automatically.",
    highlight: "propose → validate → promote",
    tone: "brand" as const,
  },
  {
    icon: "🛡",
    badge: "Risk-first",
    title: "Safe by default",
    body:
      "Paper mode is the runtime's home base. Live trades pass through policy, approval queue, drift detection, and execution gates before any capital moves.",
    highlight: "policy → approval → gate",
    tone: "fluid" as const,
  },
  {
    icon: "◈",
    badge: "Skill graph",
    title: "Composable",
    body:
      "Skills are markdown + tool definitions. Agents load them on demand, recombine into strategies, and trace every decision back to the skill that shaped it.",
    highlight: "skill × agent × strategy",
    tone: "ok" as const,
  },
];

const TONE_GRADIENTS: Record<string, string> = {
  brand: "linear-gradient(135deg, rgba(139,92,246,0.35), rgba(88,28,135,0.15))",
  fluid: "linear-gradient(135deg, rgba(34,211,238,0.32), rgba(8,145,178,0.12))",
  ok: "linear-gradient(135deg, rgba(16,217,147,0.28), rgba(6,95,70,0.1))",
};

const TONE_BORDERS: Record<string, string> = {
  brand: "rgba(139,92,246,0.45)",
  fluid: "rgba(34,211,238,0.4)",
  ok: "rgba(16,217,147,0.38)",
};

export function LandingFeatures() {
  return (
    <section className="relative py-24 px-6 lg:px-10">
      <div className="mx-auto max-w-6xl">
        {/* Section header · 滚动到视口时从下浮入 */}
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="text-center mb-14"
        >
          <div className="inline-flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.28em] text-fluid-300 mb-4">
            <span className="w-1 h-1 rounded-full bg-fluid-400 shadow-[0_0_8px_rgba(34,211,238,0.8)]" />
            <span>Why Nerya</span>
          </div>
          <h2 className="text-[32px] md:text-[44px] font-semibold tracking-tight text-gradient-brand leading-[1.1] max-w-3xl mx-auto">
            A runtime that learns to trade
            <br />
            — then learns to trade{" "}
            <em className="not-italic text-fluid-300">better</em>.
          </h2>
          {/* 下划扫光（section header 的锚点） */}
          <motion.div
            initial={{ scaleX: 0 }}
            whileInView={{ scaleX: 1 }}
            viewport={{ once: true }}
            transition={{ duration: 0.8, delay: 0.3 }}
            className="mx-auto mt-6 h-[2px] w-24 rounded-full origin-center"
            style={{
              background:
                "linear-gradient(90deg, transparent, rgba(180,139,255,0.9), rgba(34,211,238,0.9), transparent)",
            }}
          />
          <p className="mt-4 text-ink-400 text-[14px] max-w-2xl mx-auto leading-relaxed">
            Three primitives, one closed loop. The agent operates the market,
            the evolution engine rewrites the agent.
          </p>
        </motion.div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
          {FEATURES.map((f, i) => (
            <FeatureCard key={f.title} feature={f} index={i} />
          ))}
        </div>
      </div>
    </section>
  );
}

function FeatureCard({
  feature,
  index,
}: {
  feature: (typeof FEATURES)[number];
  index: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const rawX = useMotionValue(0);
  const rawY = useMotionValue(0);
  const x = useSpring(rawX, { stiffness: 180, damping: 20 });
  const y = useSpring(rawY, { stiffness: 180, damping: 20 });
  const rotateX = useTransform(y, [-1, 1], [6, -6]);
  const rotateY = useTransform(x, [-1, 1], [-6, 6]);

  function handleMouseMove(e: MouseEvent<HTMLDivElement>) {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const cx = (e.clientX - rect.left) / rect.width - 0.5;
    const cy = (e.clientY - rect.top) / rect.height - 0.5;
    rawX.set(cx * 2);
    rawY.set(cy * 2);
  }
  function handleMouseLeave() {
    rawX.set(0);
    rawY.set(0);
  }

  return (
    <motion.div
      ref={ref}
      initial={{ opacity: 0, y: 60, rotateZ: index % 2 === 0 ? -2 : 2 }}
      whileInView={{ opacity: 1, y: 0, rotateZ: 0 }}
      viewport={{ once: true, margin: "-80px" }}
      transition={{
        duration: 0.7,
        delay: index * 0.12,
        ease: [0.22, 0.61, 0.36, 1],
      }}
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
      style={{
        rotateX,
        rotateY,
        transformStyle: "preserve-3d",
        transformPerspective: 1200,
      }}
      className="relative rounded-2xl border backdrop-blur-glass p-6 overflow-hidden group"
    >
      {/* 背景渐变 */}
      <div
        className="absolute inset-0 opacity-90 pointer-events-none"
        style={{ background: TONE_GRADIENTS[feature.tone] }}
      />
      {/* 顶部扫光描边 · 进入视口时从左扫到右 */}
      <motion.div
        className="absolute inset-x-0 top-0 h-[1.5px] pointer-events-none origin-left"
        initial={{ scaleX: 0, opacity: 0 }}
        whileInView={{ scaleX: 1, opacity: 1 }}
        viewport={{ once: true }}
        transition={{ duration: 0.9, delay: 0.4 + index * 0.12 }}
        style={{
          background: `linear-gradient(90deg, transparent, ${TONE_BORDERS[feature.tone]}, transparent)`,
        }}
      />
      {/* 内容 */}
      <div className="relative z-10">
        <div className="flex items-center justify-between mb-5">
          <div
            className="inline-flex items-center justify-center w-10 h-10 rounded-xl border backdrop-blur-md text-xl"
            style={{
              borderColor: TONE_BORDERS[feature.tone],
              background: "rgba(0,0,0,0.25)",
            }}
          >
            {feature.icon}
          </div>
          <div className="text-[9px] font-mono uppercase tracking-[0.24em] text-ink-400">
            {feature.badge}
          </div>
        </div>
        <h3 className="text-[22px] font-semibold text-white tracking-tight">
          {feature.title}
        </h3>
        <p className="mt-3 text-[13px] text-ink-300 leading-relaxed">
          {feature.body}
        </p>
        <div className="mt-5 pt-4 border-t border-white/5">
          <div className="font-mono text-[11px] text-ink-200 tracking-wide">
            {feature.highlight}
          </div>
        </div>
      </div>
    </motion.div>
  );
}

export function LandingArchitecture(): ReactNode {
  return null;
}
