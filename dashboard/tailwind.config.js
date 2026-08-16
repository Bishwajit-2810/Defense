/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // The full emerald ramp. Only 50/100/500/600/900 were defined, but the
        // dashboard reaches for 200, 300, 400, 700, 800 and 950 in ~40 places —
        // every one of those emitted no CSS at all, so those elements rendered
        // unstyled. Tailwind cannot warn about a colour that isn't in the
        // theme: the class is simply never generated.
        brand: {
          50:  '#ecfdf5',
          100: '#d1fae5',
          200: '#a7f3d0',
          300: '#6ee7b7',
          400: '#34d399',
          500: '#10b981',
          600: '#059669',
          700: '#047857',
          800: '#065f46',
          900: '#064e3b',
          950: '#022c22',
        }
      }
    },
  },
  plugins: [],
}
