"use client";

import { motion } from "framer-motion";

/**
 * Landing 中央"舞台" — 参考 Valorant / GlobalLaunch / Payment 等
 * 顶级 Web3 landing 的构图：
 *
 *   - 大弧形光晕（底部径向 + 两侧收束）
 *   - SVG 弧线辐射（从中心底部向上发散）
 *   - 中央的 mock dashboard 卡片（展示产品）
 *   - 产品上方一道扫光 + 下方光束
 *
 * 整个舞台就是：把"你的 dashboard"放进一个发光的剧场里。
 */
export function LandingStage() {
  return (
    <div className="relative w-full max-w-5xl mx-auto">
      {/* ──── 大背景光晕（顶部向下的光束 + 底部大光环）──── */}
      {/* 中央向下的聚光束（从按钮位置往舞台打光） */}
      <div
        className="absolute left-1/2 top-0 -translate-x-1/2 pointer-events-none"
        style={{
          width: "60%",
          height: "160%",
          background:
            "radial-gradient(ellipse 50% 50% at 50% 20%, rgba(180,139,255,0.5), transparent 65%)",
          filter: "blur(40px)",
          zIndex: 0,
        }}
      />

      {/* ──── SVG 弧线辐射 ──── */}
      <svg
        className="absolute inset-0 w-full h-full pointer-events-none"
        viewBox="0 0 1000 520"
        preserveAspectRatio="none"
        style={{ zIndex: 1 }}
      >
        <defs>
          <linearGradient id="stage-arc-grad" x1="50%" y1="100%" x2="50%" y2="0%">
            <stop offset="0%" stopColor="rgba(180,139,255,0)" />
            <stop offset="40%" stopColor="rgba(180,139,255,0.55)" />
            <stop offset="100%" stopColor="rgba(180,139,255,0)" />
          </linearGradient>
          <radialGradient id="stage-halo" cx="50%" cy="100%" r="70%">
            <stop offset="0%" stopColor="rgba(139,92,246,0.5)" />
            <stop offset="40%" stopColor="rgba(139,92,246,0.2)" />
            <stop offset="100%" stopColor="rgba(139,92,246,0)" />
          </radialGradient>
        </defs>

        {/* 底部大光环填充（halo） */}
        <ellipse cx="500" cy="520" rx="480" ry="280" fill="url(#stage-halo)" />

        {/* 向上辐射的弧线组（13 条，随距离淡出） */}
        {Array.from({ length: 13 }, (_, i) => {
          const offset = (i - 6) * 60;
          const opacity = 0.15 + (1 - Math.abs(i - 6) / 6) * 0.4;
          return (
            <motion.path
              key={`arc-${i}`}
              d={`M ${500 + offset} 520 Q ${500 + offset * 0.6} 260 ${500 + offset * 1.4} 60`}
              stroke="url(#stage-arc-grad)"
              strokeWidth="1"
              fill="none"
              initial={{ pathLength: 0, opacity: 0 }}
              animate={{ pathLength: 1, opacity }}
              transition={{
                duration: 1.5,
                delay: 0.3 + Math.abs(i - 6) * 0.08,
                ease: "easeOut",
              }}
            />
          );
        })}

        {/* 两道流动的水平装饰线 */}
        <motion.line
          x1="0"
          y1="510"
          x2="1000"
          y2="510"
          stroke="rgba(139,92,246,0.35)"
          strokeWidth="1"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 1.8, delay: 0.6 }}
        />
        <motion.line
          x1="0"
          y1="514"
          x2="1000"
          y2="514"
          stroke="rgba(34,211,238,0.15)"
          strokeWidth="1"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 2, delay: 0.8 }}
        />
      </svg>

      {/* ──── 中央 mock dashboard 卡片 ──── */}
      <motion.div
        initial={{ opacity: 0, y: 40, scale: 0.95 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.8, delay: 0.4, ease: [0.22, 0.61, 0.36, 1] }}
        className="relative z-10 mx-auto max-w-3xl"
      >
        <MockDashboard />
      </motion.div>

      {/* ──── 浮动 stat 卡片（相对舞台定位）──── */}
      <FloatingStat
        className="absolute left-0 top-[22%] z-20 hidden md:block"
        label="SELF-IMPROVEMENTS"
        value="1,247"
        sub="auto-proposed this week"
        tone="brand"
        delay={0.9}
      />
      <FloatingStat
        className="absolute right-0 top-[18%] z-20 hidden md:block"
        label="BACKTEST COVERAGE"
        value="98.4%"
        sub="strategies validated before promote"
        tone="fluid"
        delay={1.05}
      />
      <FloatingStat
        className="absolute left-4 bottom-[14%] z-20 hidden lg:block"
        label="LIVE GATES BLOCKED"
        value="∞"
        sub="risk-first execution"
        tone="ok"
        delay={1.2}
      />

      {/* 右下角：小 ticker */}
      <motion.div
        initial={{ opacity: 0, x: 20 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.6, delay: 1.3 }}
        className="absolute right-4 bottom-[8%] z-20 hidden lg:flex items-center gap-2 rounded-md border border-[#ef4560]/30 bg-black/40 backdrop-blur-md px-3 py-1.5 font-mono text-[10px]"
      >
        <span className="text-ink-200">AVAXUSDT</span>
        <span className="text-[#ef4560]">38.00</span>
        <span className="text-[#ef4560]">▼ 0.31%</span>
      </motion.div>
    </div>
  );
}

function MockDashboard() {
  return (
    <div
      className="relative rounded-2xl border border-white/10 backdrop-blur-xl overflow-hidden"
      style={{
        background:
          "linear-gradient(145deg, rgba(30,30,48,0.88), rgba(10,11,26,0.92))",
        boxShadow:
          "0 0 0 1px rgba(139,92,246,0.18), 0 30px 80px -20px rgba(139,92,246,0.45), inset 0 1px 0 rgba(255,255,255,0.08)",
      }}
    >
      {/* 顶部 window chrome */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-white/5">
        <div className="flex items-center gap-2">
          <span className="w-2.5 h-2.5 rounded-full bg-[#ef4560]/70" />
          <span className="w-2.5 h-2.5 rounded-full bg-[#f5a524]/70" />
          <span className="w-2.5 h-2.5 rounded-full bg-accent-400/70" />
          <span className="ml-3 text-[10px] font-mono text-ink-500 uppercase tracking-[0.22em]">
            nerya.kernel
          </span>
        </div>
        <div className="flex items-center gap-2 text-[10px] font-mono text-ink-500">
          <span className="relative flex h-1.5 w-1.5">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-accent-400" />
          </span>
          <span className="text-accent-400 tracking-widest">LIVE</span>
        </div>
      </div>

      {/* 卡片内容 */}
      <div className="px-5 md:px-8 py-6 md:py-8">
        <div className="flex items-start justify-between gap-6">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-[0.28em] text-brand-300">
              Portfolio equity · v1.3.3
            </div>
            <div className="mt-2 text-[36px] md:text-[44px] font-bold text-white tracking-tight leading-none">
              $55,186
              <span className="text-ink-400 text-[18px] font-normal">.00</span>
            </div>
            <div className="mt-2 flex items-center gap-2 text-[12px]">
              <span className="text-accent-400 font-semibold">+$2,384.12</span>
              <span className="text-accent-400">↑ 4.51%</span>
              <span className="text-ink-500 font-mono">vs v1.3.2</span>
            </div>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {["v1.1", "v1.2", "v1.3", "live"].map((p, i) => (
              <button
                key={p}
                className={`text-[10px] font-mono px-2.5 py-1 rounded-md transition-colors ${
                  i === 2
                    ? "bg-brand-500/30 text-brand-100 border border-brand-500/40"
                    : i === 3
                    ? "text-accent-400 border border-accent-400/30 bg-accent-400/5"
                    : "text-ink-500 hover:text-ink-200 border border-transparent"
                }`}
              >
                {p === "live" ? (
                  <span className="flex items-center gap-1">
                    <span className="w-1 h-1 rounded-full bg-accent-400 animate-pulse" />
                    {p}
                  </span>
                ) : (
                  p
                )}
              </button>
            ))}
          </div>
        </div>

        {/* ──── 策略进化曲线 ──── */}
        <EvolutionCurve />

        {/* ──── 进化小字行 ──── */}
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-[10px] font-mono text-ink-500 border-t border-white/5 pt-4">
          <div className="flex items-center gap-4">
            <span className="flex items-center gap-1.5 text-ink-300">
              <span className="w-1 h-1 rounded-full bg-accent-400 shadow-[0_0_6px_rgba(16,217,147,0.7)]" />
              <span className="text-accent-400 font-semibold">42</span>
              <span>auto-promotions this week</span>
            </span>
            <span className="hidden md:inline text-ink-500">·</span>
            <span className="hidden md:flex items-center gap-1.5">
              <span className="text-brand-200">next evolution</span>
              <span className="text-white font-semibold">3m 42s</span>
            </span>
          </div>
          <span className="flex items-center gap-1.5 text-ink-400">
            <span>risk gate</span>
            <span className="text-accent-400 font-semibold">✓ active</span>
          </span>
        </div>

        {/* 底部 pills */}
        <div className="mt-6 flex flex-wrap items-center gap-2 text-[10px] font-mono">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-accent-400/30 bg-accent-400/10 px-2 py-0.5 text-accent-400">
            <span className="w-1 h-1 rounded-full bg-accent-400 shadow-[0_0_6px_rgba(16,217,147,0.6)]" />
            agent · v1.3.3
          </span>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-brand-500/30 bg-brand-500/10 px-2 py-0.5 text-brand-200">
            tier · medium
          </span>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-fluid-400/30 bg-fluid-400/10 px-2 py-0.5 text-fluid-300">
            24 skills loaded
          </span>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.03] px-2 py-0.5 text-ink-300">
            3 strategies running
          </span>
        </div>
      </div>

      {/* 顶部扫光 */}
      <div
        className="absolute inset-x-0 top-0 h-[1.5px] pointer-events-none"
        style={{
          background:
            "linear-gradient(90deg, transparent, rgba(180,139,255,0.8), rgba(34,211,238,0.6), transparent)",
        }}
      />
    </div>
  );
}

// 策略版本进化曲线 — 每个版本是一个数据点，整体趋势向上
// 当前版本 v1.3.3（index 7）高亮，带回落 + 新高展示"进化后反弹"叙事
function EvolutionCurve() {
  // 归一化的 x / y（0-100）
  const versions = [
    { v: "v1.1.0", y: 22 },
    { v: "v1.1.3", y: 30 },
    { v: "v1.2.0", y: 44 },
    { v: "v1.2.1", y: 38 },
    { v: "v1.2.4", y: 52 },
    { v: "v1.3.0", y: 61 },
    { v: "v1.3.1", y: 55 },
    { v: "v1.3.3", y: 82 },
  ];
  const W = 800;
  const H = 160;
  const paddingX = 20;
  const paddingY = 20;
  const step = (W - paddingX * 2) / (versions.length - 1);

  const points = versions.map((p, i) => ({
    x: paddingX + i * step,
    y: H - paddingY - ((p.y - 15) / 70) * (H - paddingY * 2),
    v: p.v,
    yRaw: p.y,
  }));

  // 平滑曲线路径（简单贝塞尔）
  const linePath = points.reduce((acc, p, i) => {
    if (i === 0) return `M ${p.x} ${p.y}`;
    const prev = points[i - 1];
    const cx = (prev.x + p.x) / 2;
    return `${acc} C ${cx} ${prev.y}, ${cx} ${p.y}, ${p.x} ${p.y}`;
  }, "");

  // 面积填充路径（闭合到底部）
  const areaPath = `${linePath} L ${points[points.length - 1].x} ${H - paddingY} L ${points[0].x} ${H - paddingY} Z`;

  const current = points[points.length - 1];

  return (
    <div className="mt-6 relative">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto"
        preserveAspectRatio="none"
      >
        <defs>
          <linearGradient id="evo-area-grad" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stopColor="rgba(180,139,255,0.45)" />
            <stop offset="100%" stopColor="rgba(180,139,255,0)" />
          </linearGradient>
          <linearGradient id="evo-line-grad" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="rgba(139,92,246,0.5)" />
            <stop offset="70%" stopColor="rgba(180,139,255,1)" />
            <stop offset="100%" stopColor="rgba(34,211,238,1)" />
          </linearGradient>
          <filter id="evo-glow">
            <feGaussianBlur stdDeviation="3" result="coloredBlur" />
            <feMerge>
              <feMergeNode in="coloredBlur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* 水平网格线 */}
        {[0.25, 0.5, 0.75].map((t) => (
          <line
            key={t}
            x1={paddingX}
            x2={W - paddingX}
            y1={paddingY + (H - paddingY * 2) * t}
            y2={paddingY + (H - paddingY * 2) * t}
            stroke="rgba(139,92,246,0.08)"
            strokeDasharray="2 4"
          />
        ))}

        {/* 面积填充 */}
        <path d={areaPath} fill="url(#evo-area-grad)" opacity="0.7" />

        {/* 主曲线 */}
        <path
          d={linePath}
          stroke="url(#evo-line-grad)"
          strokeWidth="2"
          fill="none"
          strokeLinecap="round"
          filter="url(#evo-glow)"
        />

        {/* 各版本节点 */}
        {points.map((p, i) => {
          const isCurrent = i === points.length - 1;
          return (
            <g key={p.v}>
              <circle
                cx={p.x}
                cy={p.y}
                r={isCurrent ? 6 : 3}
                fill={isCurrent ? "#ffffff" : "rgba(180,139,255,0.7)"}
                filter={isCurrent ? "url(#evo-glow)" : undefined}
              />
              {isCurrent ? (
                <>
                  {/* 当前版本脉动外环 */}
                  <circle
                    cx={p.x}
                    cy={p.y}
                    r="10"
                    fill="none"
                    stroke="rgba(180,139,255,0.6)"
                    strokeWidth="1"
                  >
                    <animate
                      attributeName="r"
                      values="8;16;8"
                      dur="2s"
                      repeatCount="indefinite"
                    />
                    <animate
                      attributeName="opacity"
                      values="0.9;0;0.9"
                      dur="2s"
                      repeatCount="indefinite"
                    />
                  </circle>
                  {/* 顶部垂直光柱 */}
                  <line
                    x1={p.x}
                    y1={p.y - 4}
                    x2={p.x}
                    y2={paddingY}
                    stroke="rgba(180,139,255,0.3)"
                    strokeDasharray="2 3"
                  />
                </>
              ) : null}
              {/* 版本标签（仅奇数索引 + 最新） */}
              {i % 2 === 1 || isCurrent ? (
                <text
                  x={p.x}
                  y={H - 4}
                  textAnchor="middle"
                  fill={isCurrent ? "#b48bff" : "rgba(155,152,186,0.7)"}
                  fontSize="9"
                  fontFamily="monospace"
                  fontWeight={isCurrent ? 600 : 400}
                >
                  {p.v}
                </text>
              ) : null}
            </g>
          );
        })}

        {/* 当前版本标注 */}
        <g transform={`translate(${current.x - 58}, ${current.y - 38})`}>
          <rect
            x="0"
            y="0"
            width="116"
            height="22"
            rx="4"
            fill="rgba(10,11,26,0.85)"
            stroke="rgba(180,139,255,0.5)"
          />
          <text
            x="58"
            y="14"
            textAnchor="middle"
            fill="#b48bff"
            fontSize="9"
            fontFamily="monospace"
            fontWeight="600"
          >
            +49% vs v1.3.1 ✓ promoted
          </text>
          <path
            d="M 58 22 L 54 27 L 62 27 Z"
            fill="rgba(180,139,255,0.5)"
          />
        </g>
      </svg>
    </div>
  );
}

function FloatingStat({
  label,
  value,
  sub,
  tone,
  delay,
  className,
}: {
  label: string;
  value: string;
  sub: string;
  tone: "brand" | "fluid" | "ok";
  delay: number;
  className?: string;
}) {
  const toneClass = {
    brand: "text-brand-200",
    fluid: "text-fluid-300",
    ok: "text-accent-400",
  }[tone];
  return (
    <motion.div
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.6, delay }}
      className={className}
    >
      <motion.div
        animate={{ y: [0, -8, 0] }}
        transition={{ duration: 5, repeat: Infinity, ease: "easeInOut" }}
        className="rounded-xl border border-white/10 backdrop-blur-xl px-4 py-3 max-w-[220px]"
        style={{
          background:
            "linear-gradient(145deg, rgba(30,30,48,0.85), rgba(10,11,26,0.9))",
          boxShadow:
            "0 0 0 1px rgba(139,92,246,0.12), 0 12px 32px -8px rgba(0,0,0,0.5)",
        }}
      >
        <div className="text-[9px] font-mono uppercase tracking-[0.24em] text-ink-500">
          {label}
        </div>
        <div className={`mt-1 text-[22px] font-bold leading-tight ${toneClass}`}>
          {value}
        </div>
        <div className="mt-1 text-[10px] text-ink-400 leading-snug">{sub}</div>
      </motion.div>
    </motion.div>
  );
}
