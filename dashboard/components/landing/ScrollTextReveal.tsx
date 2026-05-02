"use client";

import { motion, useScroll, useTransform, type MotionValue } from "framer-motion";
import { useRef, type ReactNode } from "react";

/**
 * 滚动驱动的多行文本逐行浮现组件。
 *
 * 将每一行按 scroll 进度映射到 opacity + y 变换，
 * 用户滚动时逐行"亮"起来，像在朗读。
 */
export function ScrollTextReveal({
  lines,
  className = "",
  lineClassName = "",
}: {
  lines: ReactNode[];
  className?: string;
  lineClassName?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ["start 85%", "end 50%"],
  });

  return (
    <div ref={ref} className={className}>
      {lines.map((line, i) => (
        <RevealLine
          key={i}
          line={line}
          index={i}
          total={lines.length}
          progress={scrollYProgress}
          className={lineClassName}
        />
      ))}
    </div>
  );
}

function RevealLine({
  line,
  index,
  total,
  progress,
  className,
}: {
  line: ReactNode;
  index: number;
  total: number;
  progress: MotionValue<number>;
  className: string;
}) {
  // 每一行占 scroll 进度的一段，前后略有重叠（流畅感）
  const step = 0.9 / total;
  const start = index * step;
  const end = start + step * 1.3;

  const opacity = useTransform(progress, [start, end], [0.15, 1]);
  const y = useTransform(progress, [start, end], [20, 0]);
  const filter = useTransform(
    progress,
    [start, end],
    ["blur(6px)", "blur(0px)"],
  );

  return (
    <motion.div
      style={{ opacity, y, filter }}
      className={className}
    >
      {line}
    </motion.div>
  );
}
