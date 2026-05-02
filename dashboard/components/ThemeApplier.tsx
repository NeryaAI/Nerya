"use client";

import { useEffect } from "react";
import { useUiSettings } from "../lib/settings";

/**
 * 把 settings.darkMode 同步到 <html> 的 class。
 * darkMode=true  → 移除 "light"（默认暗色）
 * darkMode=false → 加上 "light"（亮色模式）
 */
export function ThemeApplier() {
  const [settings] = useUiSettings();

  useEffect(() => {
    const html = document.documentElement;
    if (settings.darkMode) {
      html.classList.remove("light");
    } else {
      html.classList.add("light");
    }
  }, [settings.darkMode]);

  return null;
}
