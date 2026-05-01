import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Dark violet/slate "ink" surface palette. Slightly warmer / more
        // violet than neutral grey so the violet accents feel native.
        // 950 added so OLED-true-black surfaces have a name.
        ink: {
          50: "#f6f5fb",
          100: "#e7e5f2",
          200: "#c9c7db",
          300: "#9c98ba",
          400: "#6b6a85",
          500: "#454560",
          600: "#2a2a3e",
          700: "#1e1e30",
          800: "#131426",
          900: "#0a0b1a",
          950: "#04040d",
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
          "InterVariable",
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
      },
      boxShadow: {
        glow: "0 0 24px -6px rgba(139, 92, 246, 0.45)",
        "glow-lg":
          "0 0 48px -12px rgba(139, 92, 246, 0.55), 0 0 16px -2px rgba(34, 211, 238, 0.18)",
        neon: "0 0 18px -2px rgba(16, 217, 147, 0.55)",
        // Liquid glass — soft inner highlight + outer depth.
        glass:
          "inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 1px 2px rgba(0, 0, 0, 0.4), 0 24px 48px -24px rgba(0, 0, 0, 0.7)",
      },
      backdropBlur: {
        glass: "20px",
      },
      animation: {
        "ai-pulse": "ai-pulse 1.4s ease-in-out infinite",
        "aurora-shift": "aurora-shift 18s ease-in-out infinite",
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
        "aurora-shift": {
          "0%, 100%": {
            transform: "translate(0%, 0%) rotate(0deg)",
          },
          "33%": {
            transform: "translate(2%, -1%) rotate(40deg)",
          },
          "66%": {
            transform: "translate(-1%, 2%) rotate(-30deg)",
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
