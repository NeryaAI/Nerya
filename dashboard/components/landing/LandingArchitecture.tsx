"use client";

import { motion, useScroll, useTransform, type MotionValue } from "framer-motion";
import { useRef } from "react";

const NODES = [
  { id: "market", label: "Market", sub: "data · signals · ticks", x: 100, y: 100 },
  { id: "agent", label: "Agent", sub: "route · think · act", x: 400, y: 60 },
  { id: "skill", label: "Skill graph", sub: "tools · md · permissions", x: 680, y: 110 },
  { id: "risk", label: "Risk gate", sub: "policy · approval · limits", x: 680, y: 280 },
  { id: "execute", label: "Execute", sub: "paper · live · drift check", x: 400, y: 340 },
  { id: "evolve", label: "Evolution", sub: "propose · validate · promote", x: 100, y: 280 },
];

const EDGES = [
  { from: "market", to: "agent" },
  { from: "agent", to: "skill" },
  { from: "skill", to: "risk" },
  { from: "risk", to: "execute" },
  { from: "execute", to: "evolve" },
  { from: "evolve", to: "agent" },
];

function nodeById(id: string) {
  return NODES.find((n) => n.id === id)!;
}

export function LandingArchitecture() {
  const sectionRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: sectionRef,
    offset: ["start end", "center center"],
  });

  return (
    <section ref={sectionRef} className="relative py-24 px-6 lg:px-10">
      <div className="mx-auto max-w-6xl">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="text-center mb-14"
        >
          <div className="inline-flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.28em] text-brand-300 mb-4">
            <span className="w-1 h-1 rounded-full bg-brand-400 shadow-[0_0_8px_rgba(139,92,246,0.8)]" />
            <span>Closed loop</span>
          </div>
          <h2 className="text-[32px] md:text-[44px] font-semibold tracking-tight text-gradient-brand leading-[1.1]">
            Market turns the agent.
            <br />
            The agent turns itself.
          </h2>
          <motion.div
            initial={{ scaleX: 0 }}
            whileInView={{ scaleX: 1 }}
            viewport={{ once: true }}
            transition={{ duration: 0.8, delay: 0.3 }}
            className="mx-auto mt-6 h-[2px] w-24 rounded-full origin-center"
            style={{
              background:
                "linear-gradient(90deg, transparent, rgba(139,92,246,0.9), rgba(180,139,255,0.9), transparent)",
            }}
          />
          <p className="mt-4 text-ink-400 text-[14px] max-w-2xl mx-auto leading-relaxed">
            Every trade feeds the evolution engine. Every evolution re-enters
            the loop as a new version of the agent.
          </p>
        </motion.div>

        <motion.div
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true, margin: "-80px" }}
          transition={{ duration: 0.6 }}
          className="glass rounded-2xl p-6 md:p-10 overflow-hidden"
        >
          <div className="relative">
            <svg
              viewBox="0 0 780 400"
              className="w-full h-auto"
              preserveAspectRatio="xMidYMid meet"
            >
              <defs>
                <filter id="arch-glow">
                  <feGaussianBlur stdDeviation="3" result="coloredBlur" />
                  <feMerge>
                    <feMergeNode in="coloredBlur" />
                    <feMergeNode in="SourceGraphic" />
                  </feMerge>
                </filter>
              </defs>

              {/* 边 — 透明度随 scroll 顺序点亮 */}
              {EDGES.map((e, i) => (
                <ScrollLinkedEdge
                  key={`edge-${e.from}-${e.to}`}
                  from={nodeById(e.from)}
                  to={nodeById(e.to)}
                  index={i}
                  total={EDGES.length}
                  progress={scrollYProgress}
                />
              ))}

              {/* 节点 — 滚动时依次点亮颜色 */}
              {NODES.map((n, i) => (
                <ScrollLinkedNode
                  key={n.id}
                  node={n}
                  index={i}
                  total={NODES.length}
                  progress={scrollYProgress}
                />
              ))}
            </svg>

            <div className="mt-6 grid grid-cols-1 md:grid-cols-3 gap-4 text-[11px]">
              <div className="flex items-center gap-2 text-ink-400">
                <span className="w-2 h-2 rounded-full bg-brand-400 shadow-[0_0_6px_rgba(139,92,246,0.6)]" />
                <span>
                  <span className="text-ink-100 font-semibold">Agents</span> operate the loop
                </span>
              </div>
              <div className="flex items-center gap-2 text-ink-400">
                <span className="w-2 h-2 rounded-full bg-fluid-400 shadow-[0_0_6px_rgba(34,211,238,0.6)]" />
                <span>
                  <span className="text-ink-100 font-semibold">Skills</span> are the atoms
                </span>
              </div>
              <div className="flex items-center gap-2 text-ink-400">
                <span className="w-2 h-2 rounded-full bg-accent-400 shadow-[0_0_6px_rgba(16,217,147,0.6)]" />
                <span>
                  <span className="text-ink-100 font-semibold">Evolution</span> rewrites the agent
                </span>
              </div>
            </div>
          </div>
        </motion.div>
      </div>
    </section>
  );
}

function ScrollLinkedEdge({
  from,
  to,
  index,
  total,
  progress,
}: {
  from: { x: number; y: number };
  to: { x: number; y: number };
  index: number;
  total: number;
  progress: MotionValue<number>;
}) {
  // 边的"点亮进度"：随 scroll 顺序从暗到亮
  const step = 0.65 / total;
  const start = 0.15 + step * index;
  const end = start + step;

  const opacity = useTransform(progress, [start, end], [0.15, 1]);
  const strokeWidth = useTransform(progress, [start, end], [1, 2]);
  // 光点的可见度跟随边的点亮度（边亮了，光点才显示）
  const pointOpacity = useTransform(progress, [start - 0.05, start + 0.05], [0, 1]);

  return (
    <g>
      <motion.line
        x1={from.x}
        y1={from.y}
        x2={to.x}
        y2={to.y}
        stroke="#b48bff"
        strokeDasharray="4 4"
        style={{ opacity, strokeWidth }}
      />
      {/* 流动光点 — 边点亮后开始连续来回流动 */}
      <motion.circle
        r="4"
        fill="#b48bff"
        filter="url(#arch-glow)"
        style={{ opacity: pointOpacity }}
        animate={{
          cx: [from.x, to.x],
          cy: [from.y, to.y],
        }}
        transition={{
          duration: 2.4,
          delay: index * 0.4,
          repeat: Infinity,
          ease: "easeInOut",
        }}
      />
    </g>
  );
}

function ScrollLinkedNode({
  node,
  index,
  total,
  progress,
}: {
  node: { x: number; y: number; label: string; sub: string };
  index: number;
  total: number;
  progress: MotionValue<number>;
}) {
  const step = 0.7 / total;
  const start = 0.1 + step * index;
  const end = start + step;

  const strokeOpacity = useTransform(progress, [start, end], [0.2, 1]);
  const fillOpacity = useTransform(progress, [start, end], [0.3, 1]);
  const dotOpacity = useTransform(progress, [start, end], [0.3, 1]);
  const dotScale = useTransform(progress, [start, end], [0.6, 1]);

  return (
    <motion.g
      initial={{ opacity: 0, scale: 0.7 }}
      whileInView={{ opacity: 1, scale: 1 }}
      viewport={{ once: true }}
      transition={{ duration: 0.4, delay: index * 0.08 }}
    >
      <motion.circle
        cx={node.x}
        cy={node.y}
        r="38"
        fill="rgba(10,11,26,0.9)"
        stroke="#b48bff"
        style={{ strokeOpacity, strokeWidth: 1.5 }}
        filter="url(#arch-glow)"
      />
      <motion.circle
        cx={node.x}
        cy={node.y}
        r="6"
        fill="#b48bff"
        style={{ opacity: dotOpacity, scale: dotScale, originX: node.x, originY: node.y }}
      />
      <motion.text
        x={node.x}
        y={node.y + 58}
        textAnchor="middle"
        fill="#e7e5f2"
        fontSize="13"
        fontWeight="600"
        style={{ opacity: fillOpacity }}
      >
        {node.label}
      </motion.text>
      <motion.text
        x={node.x}
        y={node.y + 76}
        textAnchor="middle"
        fill="#6b6a85"
        fontSize="10"
        fontFamily="monospace"
        style={{ opacity: fillOpacity }}
      >
        {node.sub}
      </motion.text>
    </motion.g>
  );
}
