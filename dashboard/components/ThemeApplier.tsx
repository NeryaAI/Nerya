"use client";

import { useEffect } from "react";
import { isDarkThemeMode, useUiSettings } from "../lib/settings";

/**
 * 把 settings.darkMode 同步到 <html> 的 class。
 * light/dark/system 三档支持：
 * - light  → 添加 "light"（亮色）
 * - dark   → 不添加（暗色）
 * - system → 跟随系统 prefers-color-scheme 并监听系统切换
 */
export function ThemeApplier() {
  const [settings] = useUiSettings();

  useEffect(() => {
    const html = document.documentElement;
    const applyTheme = () => {
      const isDark = isDarkThemeMode(settings.darkMode);
      html.classList.toggle("light", !isDark);
    };

    applyTheme();

    if (settings.darkMode !== "system") {
      return;
    }

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    if ("addEventListener" in media) {
      media.addEventListener("change", applyTheme);
      return () => media.removeEventListener("change", applyTheme);
    }

    const legacyMedia = media as MediaQueryList & {
      addListener?: (listener: () => void) => void;
      removeListener?: (listener: () => void) => void;
    };
    legacyMedia.addListener?.(applyTheme);
    return () => legacyMedia.removeListener?.(applyTheme);
  }, [settings.darkMode]);

  return null;
}
