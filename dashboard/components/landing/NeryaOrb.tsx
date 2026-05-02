"use client";

import { motion } from "framer-motion";

/**
 * 中央发光球体 — Web3 landing 的视觉核心
 *
 * 结构（由外到内）：
 *   - 3 圈动态旋转光环（不同速度/方向的 conic-gradient）
 *   - 紫色发光描边 + 白色内描边（给球体清晰边缘）
 *   - 实心核心（径向渐变球体 + 顶部高光 + 脉动核心）
 *   - 单个紫色"信号"光点沿球面边缘轨道环绕
 *   - 对位的间歇性白色 flash（呼应主脉冲）
 *
 * 纯 CSS/Framer Motion 实现，GPU 加速。
 */
export function NeryaOrb({ size = 340 }: { size?: number }) {
  return (
    <div className="relative" style={{ width: size, height: size }}>
      {/* ──── 外圈光环 ──── */}
      {/* 最外层：紫色主轴，18s 正转 */}
      <motion.div
        className="absolute inset-0 rounded-full"
        style={{
          background:
            "conic-gradient(from 0deg, rgba(139,92,246,0) 0deg, rgba(139,92,246,0.85) 90deg, rgba(34,211,238,0.75) 180deg, rgba(180,139,255,0) 260deg, rgba(139,92,246,0) 360deg)",
          filter: "blur(18px)",
          opacity: 0.65,
        }}
        animate={{ rotate: 360 }}
        transition={{ duration: 18, repeat: Infinity, ease: "linear" }}
      />

      {/* 中间层：青色反转，12s */}
      <motion.div
        className="absolute rounded-full"
        style={{
          inset: "8%",
          background:
            "conic-gradient(from 120deg, rgba(34,211,238,0) 0deg, rgba(34,211,238,0.9) 110deg, rgba(139,92,246,0.6) 220deg, rgba(34,211,238,0) 360deg)",
          filter: "blur(10px)",
          opacity: 0.8,
        }}
        animate={{ rotate: -360 }}
        transition={{ duration: 12, repeat: Infinity, ease: "linear" }}
      />

      {/* 细节环：尖锐边缘，8s 正转 */}
      <motion.div
        className="absolute rounded-full border border-white/20"
        style={{
          inset: "14%",
          background:
            "conic-gradient(from 60deg, transparent 0deg, rgba(255,255,255,0.1) 90deg, transparent 180deg, rgba(180,139,255,0.15) 270deg, transparent 360deg)",
        }}
        animate={{ rotate: 360 }}
        transition={{ duration: 8, repeat: Infinity, ease: "linear" }}
      />

      {/* ──── 球体核心 ──── */}
      <div
        className="absolute rounded-full shadow-[0_0_80px_20px_rgba(139,92,246,0.45)]"
        style={{
          inset: "22%",
          background:
            "radial-gradient(circle at 35% 30%, rgba(200,180,255,0.95) 0%, rgba(139,92,246,0.9) 25%, rgba(88,28,135,0.95) 60%, rgba(20,10,40,1) 95%)",
        }}
      >
        {/* 顶部高光 */}
        <div
          className="absolute rounded-full pointer-events-none"
          style={{
            top: "8%",
            left: "18%",
            width: "45%",
            height: "30%",
            background:
              "radial-gradient(ellipse at center, rgba(255,255,255,0.55), transparent 70%)",
            filter: "blur(8px)",
          }}
        />
        {/* 中心脉动光点 */}
        <motion.div
          className="absolute rounded-full pointer-events-none"
          style={{
            top: "40%",
            left: "40%",
            width: "20%",
            height: "20%",
            background:
              "radial-gradient(circle, rgba(34,211,238,0.9), transparent 70%)",
            filter: "blur(6px)",
          }}
          animate={{ scale: [1, 1.4, 1], opacity: [0.6, 1, 0.6] }}
          transition={{ duration: 2.4, repeat: Infinity, ease: "easeInOut" }}
        />
      </div>

      {/* ──── 球体描边（清晰锁边） ──── */}
      {/* 外层紫色发光描边 */}
      <div
        className="absolute rounded-full border border-brand-400/50 pointer-events-none"
        style={{
          inset: "22%",
          boxShadow:
            "0 0 18px rgba(139,92,246,0.45), inset 0 0 18px rgba(139,92,246,0.25)",
        }}
      />
      {/* 内层白色细描边 */}
      <div
        className="absolute rounded-full border border-white/35 pointer-events-none"
        style={{ inset: "23%" }}
      />

      {/* ──── 主脉冲信号点 · 6s 沿球边环绕 ──── */}
      <motion.div
        className="absolute pointer-events-none"
        style={{ inset: "22%" }}
        animate={{ rotate: 360 }}
        transition={{ duration: 6, repeat: Infinity, ease: "linear" }}
      >
        {/* 主光点 */}
        <motion.div
          className="absolute rounded-full bg-brand-200"
          style={{
            top: "50%",
            right: "0%",
            width: 10,
            height: 10,
            transform: "translate(50%, -50%)",
            boxShadow:
              "0 0 14px 4px rgba(180,139,255,0.9), 0 0 28px 10px rgba(139,92,246,0.55)",
          }}
          animate={{ scale: [1, 1.25, 1], opacity: [0.9, 1, 0.9] }}
          transition={{ duration: 1.2, repeat: Infinity, ease: "easeInOut" }}
        />
        {/* 光点尾迹（小一点的弱光点跟随） */}
        <div
          className="absolute rounded-full bg-brand-300/60"
          style={{
            top: "50%",
            right: "0%",
            width: 18,
            height: 18,
            transform: "translate(70%, -50%)",
            filter: "blur(6px)",
          }}
        />
      </motion.div>

      {/* ──── 次级信号点 · 11s 反向，对位闪烁 ──── */}
      <motion.div
        className="absolute pointer-events-none"
        style={{ inset: "22%" }}
        animate={{ rotate: -360 }}
        transition={{ duration: 11, repeat: Infinity, ease: "linear" }}
      >
        <motion.div
          className="absolute rounded-full bg-white"
          style={{
            top: "50%",
            left: "0%",
            width: 6,
            height: 6,
            transform: "translate(-50%, -50%)",
            boxShadow: "0 0 10px 3px rgba(255,255,255,0.85)",
          }}
          animate={{ opacity: [0, 1, 0] }}
          transition={{
            duration: 0.6,
            repeat: Infinity,
            repeatDelay: 3.2,
            ease: "easeInOut",
          }}
        />
      </motion.div>

      {/* ──── 底部能量环呼吸 ──── */}
      <motion.div
        className="absolute rounded-full border border-brand-400/30 pointer-events-none"
        style={{ inset: "-5%" }}
        animate={{ scale: [1, 1.08, 1], opacity: [0.35, 0.55, 0.35] }}
        transition={{ duration: 4, repeat: Infinity, ease: "easeInOut" }}
      />
      <motion.div
        className="absolute rounded-full border border-fluid-400/20 pointer-events-none"
        style={{ inset: "-12%" }}
        animate={{ scale: [1.05, 1, 1.05], opacity: [0.2, 0.4, 0.2] }}
        transition={{ duration: 5.5, repeat: Infinity, ease: "easeInOut" }}
      />
    </div>
  );
}
