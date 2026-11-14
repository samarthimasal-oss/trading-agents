/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#080a0d",
          900: "#0b0e12",
          850: "#0f1319",
          800: "#131920",
          750: "#18202a",
          700: "#1f2934",
          600: "#2a3644",
        },
        mist: {
          100: "#eef1f5",
          300: "#b7c0cc",
          500: "#7e8b9b",
          600: "#647181",
        },
        gold: {
          300: "#e9c987",
          400: "#d9ae62",
          500: "#c8953f",
          600: "#a7772b",
        },
        signal: {
          up: "#4ecb9a",
          down: "#e46b76",
          warn: "#e8b45a",
          info: "#74a7d8",
        },
      },
      fontFamily: {
        display: ["Bahnschrift", "DIN Alternate", "Segoe UI", "sans-serif"],
        sans: ["Segoe UI", "Inter", "system-ui", "sans-serif"],
        mono: ["Cascadia Mono", "Consolas", "SFMono-Regular", "monospace"],
      },
      boxShadow: {
        panel: "0 18px 45px -28px rgba(0,0,0,0.9)",
        gold: "0 0 0 1px rgba(217,174,98,0.25), 0 12px 40px -20px rgba(217,174,98,0.28)",
      },
      backgroundImage: {
        grid: "linear-gradient(rgba(126,139,155,0.055) 1px, transparent 1px), linear-gradient(90deg, rgba(126,139,155,0.055) 1px, transparent 1px)",
      },
      keyframes: {
        pulseSoft: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.45" },
        },
        rise: {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        pulseSoft: "pulseSoft 2.2s ease-in-out infinite",
        rise: "rise 320ms ease-out both",
      },
    },
  },
  plugins: [],
};
