"use client";

import { motion } from "framer-motion";
import { useEffect, useState } from "react";

type Ticker = {
  symbol: string;
  base: number;
  // -1 为下跌，1 为上涨
  trend: 1 | -1;
  x: number;
  y: number;
};

const TICKERS: Ticker[] = [
  { symbol: "BTCUSDT", base: 68420, trend: 1, x: 8, y: 18 },
  { symbol: "ETHUSDT", base: 3520, trend: -1, x: 82, y: 14 },
  { symbol: "SOLUSDT", base: 182.4, trend: 1, x: 12, y: 72 },
  { symbol: "AVAXUSDT", base: 38.12, trend: -1, x: 85, y: 66 },
  { symbol: "NRYA_STRATEGY_v1.3", base: 0, trend: 1, x: 72, y: 88 },
];

type TickerValue = { price: string; pct: string };

function randomDelta(base: number, trend: number): TickerValue {
  const pct = (Math.random() * 0.004 + 0.001) * trend;
  return {
    pct: (pct * 100).toFixed(2),
    price: (base * (1 + pct)).toFixed(base > 1000 ? 0 : 2),
  };
}

export function FloatingTickers() {
  // 关键：初始值设为 null，避免 SSR 生成随机数导致 hydration 不一致
  const [values, setValues] = useState<TickerValue[] | null>(null);

  useEffect(() => {
    // 仅客户端 mount 后生成初始值
    setValues(TICKERS.map((t) => randomDelta(t.base || 100, t.trend)));

    // 之后每 2.2 秒刷新一次
    const id = setInterval(() => {
      setValues(TICKERS.map((t) => randomDelta(t.base || 100, t.trend)));
    }, 2200);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      {TICKERS.map((ticker, i) => {
        const up = ticker.trend === 1;
        const v = values?.[i];
        return (
          <motion.div
            key={ticker.symbol}
            className="absolute"
            style={{ left: `${ticker.x}%`, top: `${ticker.y}%` }}
            initial={{ opacity: 0, y: 20 }}
            animate={{
              opacity: [0.3, 0.75, 0.3],
              y: [0, -14, 0],
            }}
            transition={{
              duration: 6 + i * 0.8,
              delay: i * 0.4,
              repeat: Infinity,
              ease: "easeInOut",
            }}
          >
            <div
              className="rounded-md border backdrop-blur-md px-3 py-1.5 text-[10px] font-mono whitespace-nowrap"
              style={{
                background: up
                  ? "rgba(16, 217, 147, 0.06)"
                  : "rgba(239, 69, 96, 0.06)",
                borderColor: up
                  ? "rgba(16, 217, 147, 0.25)"
                  : "rgba(239, 69, 96, 0.25)",
              }}
            >
              <div className="flex items-center gap-2">
                <span className="text-ink-200">{ticker.symbol}</span>
                <span
                  className={up ? "text-accent-400" : "text-[#ef4560]"}
                  suppressHydrationWarning
                >
                  {v && ticker.base ? v.price : ticker.base ? "--" : ""}
                </span>
                <span
                  className={`text-[9px] ${
                    up ? "text-accent-400" : "text-[#ef4560]"
                  }`}
                  suppressHydrationWarning
                >
                  {up ? "▲" : "▼"} {v ? v.pct : "0.00"}%
                </span>
              </div>
            </div>
          </motion.div>
        );
      })}
    </div>
  );
}
