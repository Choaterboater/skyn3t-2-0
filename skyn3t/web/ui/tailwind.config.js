/** @type {import('tailwindcss').Config} */
// SkyN3t "Foundry" design system. Activity reads as heat: things glow EMBER
// when working, cool to PLASMA when idle. SYNAPSE (violet) is reserved for the
// brain/learning surface only. Boldness is spent in one place — the heat.
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        void: "rgb(var(--c-canvas) / <alpha-value>)",
        panel: "rgb(var(--c-surface) / <alpha-value>)",
        "panel-2": "rgb(var(--c-surface-2) / <alpha-value>)",
        hairline: "rgb(var(--c-border) / <alpha-value>)",
        ember: { DEFAULT: "rgb(var(--c-accent) / <alpha-value>)", soft: "rgb(var(--c-accent-soft) / <alpha-value>)", deep: "rgb(var(--c-accent-deep) / <alpha-value>)" },
        plasma: { DEFAULT: "rgb(var(--c-success) / <alpha-value>)", soft: "rgb(var(--c-success-soft) / <alpha-value>)", deep: "rgb(var(--c-success-deep) / <alpha-value>)" },
        synapse: { DEFAULT: "rgb(var(--c-synapse) / <alpha-value>)", soft: "rgb(var(--c-synapse-soft) / <alpha-value>)" },
        bone: "rgb(var(--c-text) / <alpha-value>)",
        ash: "rgb(var(--c-muted) / <alpha-value>)",
        // keep `brand` as an alias so any stray reference still resolves
        brand: { DEFAULT: "#FF6A3D", dim: "#C2410C" },
      },
      fontFamily: {
        display: ['"Space Grotesk"', "system-ui", "sans-serif"],
        sans: ['"Inter"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      letterSpacing: { eyebrow: "0.22em" },
      boxShadow: {
        ember: "0 0 0 1px rgba(255,106,61,0.35), 0 0 22px -4px rgba(255,106,61,0.55)",
        plasma: "0 0 0 1px rgba(61,217,196,0.30), 0 0 18px -6px rgba(61,217,196,0.45)",
        panel: "0 1px 0 0 rgba(255,255,255,0.03) inset, 0 10px 30px -20px rgba(0,0,0,0.8)",
      },
      keyframes: {
        forgepulse: {
          "0%,100%": { opacity: "0.45", transform: "scale(1)" },
          "50%": { opacity: "1", transform: "scale(1.15)" },
        },
        emberflare: {
          "0%,100%": { boxShadow: "0 0 12px -4px rgba(255,106,61,0.5)" },
          "50%": { boxShadow: "0 0 26px -2px rgba(255,106,61,0.9)" },
        },
        risefade: {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        sweep: {
          "0%": { transform: "translateX(-120%)" },
          "100%": { transform: "translateX(120%)" },
        },
      },
      animation: {
        forgepulse: "forgepulse 2.2s ease-in-out infinite",
        emberflare: "emberflare 1.8s ease-in-out infinite",
        risefade: "risefade 0.5s ease-out both",
        sweep: "sweep 2.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
