"use client";

import { motion, useMotionValue, useSpring } from "framer-motion";
import { useEffect, useState } from "react";

/**
 * 跟随鼠标的发光光标 —— Web3 landing 的标配。
 *
 * 两层结构：
 *  - 内层实心小点：精准跟随，无延迟
 *  - 外层光环：带 spring 延迟，形成拖尾感
 *  - 悬停交互元素（a/button）时光环放大
 *
 * 移动端自动隐藏（无鼠标）。
 */
export function CustomCursor() {
  const [mounted, setMounted] = useState(false);
  const [hovering, setHovering] = useState(false);
  const [touch, setTouch] = useState(false);

  const mouseX = useMotionValue(-100);
  const mouseY = useMotionValue(-100);
  const springX = useSpring(mouseX, { stiffness: 420, damping: 36, mass: 0.6 });
  const springY = useSpring(mouseY, { stiffness: 420, damping: 36, mass: 0.6 });

  useEffect(() => {
    setMounted(true);
    // 检测触摸设备
    if ("ontouchstart" in window || navigator.maxTouchPoints > 0) {
      setTouch(true);
      return;
    }

    function onMove(e: MouseEvent) {
      mouseX.set(e.clientX);
      mouseY.set(e.clientY);
    }
    function onOver(e: MouseEvent) {
      const t = e.target as HTMLElement | null;
      if (!t) return;
      const isInteractive = Boolean(
        t.closest("a, button, [role=button], input, textarea, select, [data-cursor-hover]"),
      );
      setHovering(isInteractive);
    }

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseover", onOver);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseover", onOver);
    };
  }, [mouseX, mouseY]);

  if (!mounted || touch) return null;

  return (
    <>
      {/* 外层光环 — spring 跟随，悬停交互元素时放大 */}
      <motion.div
        aria-hidden
        className="pointer-events-none fixed top-0 left-0 z-[9999] rounded-full"
        style={{
          x: springX,
          y: springY,
          translateX: "-50%",
          translateY: "-50%",
          width: hovering ? 56 : 36,
          height: hovering ? 56 : 36,
          border: `1.5px solid ${hovering ? "rgba(180,139,255,0.8)" : "rgba(139,92,246,0.45)"}`,
          backgroundColor: hovering ? "rgba(139,92,246,0.08)" : "transparent",
          boxShadow: `0 0 ${hovering ? 28 : 18}px ${hovering ? "rgba(139,92,246,0.35)" : "rgba(139,92,246,0.18)"}`,
          transition: "width 0.22s ease, height 0.22s ease, background-color 0.22s ease, border-color 0.22s ease, box-shadow 0.22s ease",
          mixBlendMode: "plus-lighter" as const,
        }}
      />
      {/* 内层精准小点 — 无延迟 */}
      <motion.div
        aria-hidden
        className="pointer-events-none fixed top-0 left-0 z-[9999] w-2 h-2 rounded-full bg-brand-300"
        style={{
          x: mouseX,
          y: mouseY,
          translateX: "-50%",
          translateY: "-50%",
          boxShadow: "0 0 10px rgba(180,139,255,0.9)",
          mixBlendMode: "plus-lighter" as const,
        }}
      />
    </>
  );
}
