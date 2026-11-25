import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        page: "#f8f8f3",
        surface: "#fffffc",
        raised: "#efefe8",
        hover: "#e6e9dd",
        line: "#daddd2",
        "line-strong": "#aab1a3",
        ink: "#202c25",
        muted: "#505e52",
        faint: "#606d60",
        accent: "#225b46",
        // Status colors are paired with a label or icon.
        ok: "#297346",
        warn: "#92600d",
        crit: "#b83c37",
        nodata: "#c4c9bd",
      },
      fontFamily: {
        display: ["var(--font-display)", "Georgia", "serif"],
        sans: ["var(--font-sans)", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      keyframes: {
        ping2: { "75%, 100%": { transform: "scale(2.2)", opacity: "0" } },
        rise: { "0%": { opacity: "0", transform: "translateY(6px)" }, "100%": { opacity: "1", transform: "none" } },
      },
      animation: {
        ping2: "ping2 1.8s cubic-bezier(0, 0, 0.2, 1) infinite",
        rise: "rise 0.45s cubic-bezier(0.22, 1, 0.36, 1) both",
      },
    },
  },
  plugins: [],
};

export default config;
