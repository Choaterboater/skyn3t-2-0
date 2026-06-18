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
        void: "#0A0E17",
        panel: "#111722",
        "panel-2": "#161E2B",
        hairline: "#222C3C",
        ember: { DEFAULT: "#FF6A3D", soft: "#FF9166", deep: "#C2410C" },
        plasma: { DEFAULT: "#3DD9C4", soft: "#7FEBDD", deep: "#0E7A6B" },
        synapse: { DEFAULT: "#A688FF", soft: "#C5B2FF" },
        bone: "#E8EDF7",
        ash: "#7E8AA0",
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
