"use client";

import { useEffect, useState } from "react";

/**
 * lightweight-charts cannot read CSS variables, so chart axis/grid
 * colors must be passed as concrete values. This hook resolves the
 * palette for the active theme and re-renders when the <html> "light"
 * class toggles (see ThemeApplier), letting chart effects rebuild
 * with readable colors in both modes.
 */
export type ChartTheme = {
  isLight: boolean;
  /** axis label color */
  text: string;
  /** grid + axis border color */
  grid: string;
};

const DARK: ChartTheme = {
  isLight: false,
  text: "rgba(202,201,225,0.72)",
  grid: "rgba(139,92,246,0.12)",
};

const LIGHT: ChartTheme = {
  isLight: true,
  text: "rgba(78,75,114,0.95)",
  grid: "rgba(108,77,222,0.14)",
};

function readTheme(): ChartTheme {
  if (typeof document === "undefined") return DARK;
  return document.documentElement.classList.contains("light") ? LIGHT : DARK;
}

export function useChartTheme(): ChartTheme {
  const [theme, setTheme] = useState<ChartTheme>(readTheme);

  useEffect(() => {
    setTheme(readTheme());
    const observer = new MutationObserver(() => setTheme(readTheme()));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => observer.disconnect();
  }, []);

  return theme;
}
