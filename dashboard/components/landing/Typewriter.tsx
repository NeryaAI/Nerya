"use client";

import { useEffect, useState } from "react";

/**
 * 打字机文本组件 — 逐字符打出一段或多段文字。
 *
 * 特性：
 *  - 支持单行 / 多行串联（传数组）
 *  - 闪烁光标（可选）
 *  - 可循环
 *  - hydration-safe：SSR 时显示空，mount 后才开始打字
 */
export function Typewriter({
  lines,
  speed = 40,
  pauseBetween = 800,
  cursor = true,
  loop = false,
  className = "",
  cursorClassName = "",
  onFinish,
}: {
  lines: string | string[];
  speed?: number;
  pauseBetween?: number;
  cursor?: boolean;
  loop?: boolean;
  className?: string;
  cursorClassName?: string;
  onFinish?: () => void;
}) {
  const allLines = Array.isArray(lines) ? lines : [lines];
  const [lineIndex, setLineIndex] = useState(0);
  const [charIndex, setCharIndex] = useState(0);
  const [finished, setFinished] = useState(false);

  useEffect(() => {
    if (finished) return;
    const current = allLines[lineIndex] ?? "";

    if (charIndex < current.length) {
      const t = setTimeout(() => setCharIndex((c) => c + 1), speed);
      return () => clearTimeout(t);
    }

    // 一行打完
    if (lineIndex < allLines.length - 1) {
      const t = setTimeout(() => {
        setLineIndex((i) => i + 1);
        setCharIndex(0);
      }, pauseBetween);
      return () => clearTimeout(t);
    }

    // 全部打完
    if (loop) {
      const t = setTimeout(() => {
        setLineIndex(0);
        setCharIndex(0);
      }, pauseBetween * 2);
      return () => clearTimeout(t);
    }

    setFinished(true);
    onFinish?.();
  }, [charIndex, lineIndex, allLines, speed, pauseBetween, loop, finished, onFinish]);

  const current = allLines[lineIndex] ?? "";
  const displayed = current.slice(0, charIndex);
  const isDone = finished && !loop;

  return (
    <span className={className}>
      {/* 已完成的行 */}
      {lineIndex > 0 && !loop
        ? allLines.slice(0, lineIndex).map((line, i) => (
            <span key={i} className="block">
              {line}
            </span>
          ))
        : null}
      {/* 当前打字中的行 */}
      <span>
        {displayed}
        {cursor && !isDone ? (
          <span
            className={`inline-block w-[0.6em] align-baseline ${cursorClassName}`}
            style={{
              animation: "typewriter-blink 1s step-end infinite",
            }}
          >
            ▌
          </span>
        ) : null}
      </span>
    </span>
  );
}
