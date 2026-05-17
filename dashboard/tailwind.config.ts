import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Ink palette — RGB-triple CSS variables so Tailwind's
        // ``<alpha-value>`` placeholder works (``bg-ink-800/70`` etc.).
        // Light/dark theming overrides the same ``--ink-XXX`` triples
        // in :root vs html.light so opacity-modifier-aware classes
        // continue to switch palette automatically.
        ink: {
          50:  "rgb(var(--ink-50)  / <alpha-value>)",
          100: "rgb(var(--ink-100) / <alpha-value>)",
          200: "rgb(var(--ink-200) / <alpha-value>)",
          300: "rgb(var(--ink-300) / <alpha-value>)",
          400: "rgb(var(--ink-400) / <alpha-value>)",
          500: "rgb(var(--ink-500) / <alpha-value>)",
          600: "rgb(var(--ink-600) / <alpha-value>)",
          700: "rgb(var(--ink-700) / <alpha-value>)",
          800: "rgb(var(--ink-800) / <alpha-value>)",
          900: "rgb(var(--ink-900) / <alpha-value>)",
          950: "rgb(var(--ink-950) / <alpha-value>)",
        },
        // Violet / electric purple — the NERYA primary accent.
        brand: {
          50: "#f3ebff",
          100: "#e3d2ff",
          200: "#c9a8ff",
          300: "#b48bff",
          400: "#9d6bff",
          500: "#8b5cf6",
          600: "#7c3aed",
          700: "#6d28d9",
          800: "#5b21b6",
          900: "#3d1569",
        },
        // Cyan / fluid AI — used as the "thinking / streaming" accent.
        // Pairs with violet brand color for the AI-native fluid look.
        fluid: {
          300: "#67e8f9",
          400: "#22d3ee",
          500: "#06b6d4",
          600: "#0891b2",
        },
        // Neon mint — used for positive PnL, "RUNNING" status, win badges.
        accent: {
          300: "#7af0bf",
          400: "#34e0a1",
          500: "#10d993",
          600: "#059669",
        },
        // Hot pink / magenta — used sparingly as a secondary chart accent.
        magenta: {
          400: "#f472b6",
          500: "#ec4899",
          600: "#db2777",
        },
        danger: "#ef4560",
        warn: "#f5a524",
        ok: "#10d993",
      },
      fontFamily: {
        sans: [
          "Plus Jakarta Sans",
          "InterVariable",
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          "Liberation Mono",
          "Courier New",
          "monospace",
        ],
        display: [
          "Plus Jakarta Sans",
          "InterVariable",
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
      },
      boxShadow: {
        // 2026-05 airy redesign: collapse glow/neon/glass/airy into a
        // single soft elevation. The class names are kept so existing
        // call sites still compile; the visuals get a consistent, calm
        // shadow instead of competing violet/cyan/green halos.
        glow: "0 1px 2px rgba(2, 6, 23, 0.06)",
        "glow-lg": "0 2px 4px rgba(2, 6, 23, 0.08)",
        neon: "0 1px 2px rgba(2, 6, 23, 0.06)",
        glass: "0 1px 2px rgba(2, 6, 23, 0.06)",
        airy: "0 1px 2px rgba(2, 6, 23, 0.06)",
      },
      backdropBlur: {
        glass: "20px",
        airy: "28px",
      },
      animation: {
        // 2026-05 redesign: drop aurora background animation.
        // Keep ai-pulse (typing indicator) and shimmer (skeleton loading)
        // as they convey meaningful streaming/loading state.
        "ai-pulse": "ai-pulse 1.4s ease-in-out infinite",
        shimmer: "shimmer 2.4s linear infinite",
      },
      keyframes: {
        "ai-pulse": {
          "0%, 100%": {
            opacity: "0.45",
            transform: "scale(0.85)",
          },
          "50%": {
            opacity: "1",
            transform: "scale(1)",
          },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
    },
  },
  plugins: [],
};

export default config;
