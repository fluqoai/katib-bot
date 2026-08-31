/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        arabic: ['"Tajawal"', '"Cairo"', 'system-ui', 'sans-serif'],
      },
      colors: {
        sage: {
          50: '#f6f7f4',
          100: '#e9ece1',
          200: '#cfd5be',
          300: '#a8b58f',
          400: '#86966a',
          500: '#6c7e51',
          600: '#546540',
          700: '#445234',
          800: '#39432d',
          900: '#303828',
        },
        linen: '#faf8f3',
        wood: {
          400: '#c7a17a',
          600: '#9b7958',
        },
      },
    },
  },
  plugins: [],
}
