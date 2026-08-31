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
          50:  '#faf3eb',
          100: '#f1e3d0',
          200: '#e0c5a1',
          300: '#cea878',
          400: '#c7a17a',
          500: '#a88054',
          600: '#9b7958',
          700: '#7c5e44',
          800: '#5d4734',
          900: '#3e3024',
        },
        ink: {
          900: '#1c2017',
          700: '#3b4131',
          500: '#5e6551',
          300: '#9ba28a',
        },
      },
      boxShadow: {
        'soft': '0 2px 8px 0 rgba(28, 32, 23, 0.04), 0 1px 2px 0 rgba(28, 32, 23, 0.06)',
        'soft-lg': '0 6px 24px 0 rgba(28, 32, 23, 0.08), 0 2px 6px 0 rgba(28, 32, 23, 0.05)',
        'sage-glow': '0 0 0 4px rgba(108, 126, 81, 0.15)',
      },
      animation: {
        'fade-in': 'fadeIn 0.4s ease-out forwards',
        'slide-up': 'slideUp 0.5s ease-out forwards',
        'pulse-soft': 'pulseSoft 1.6s ease-in-out infinite',
        'shimmer': 'shimmer 2s linear infinite',
      },
      keyframes: {
        fadeIn:    { '0%': { opacity: '0' }, '100%': { opacity: '1' } },
        slideUp:   { '0%': { opacity: '0', transform: 'translateY(12px)' }, '100%': { opacity: '1', transform: 'translateY(0)' } },
        pulseSoft: { '0%, 100%': { opacity: '0.4' }, '50%': { opacity: '1' } },
        shimmer:   { '0%': { backgroundPosition: '-200% 0' }, '100%': { backgroundPosition: '200% 0' } },
      },
    },
  },
  plugins: [],
}
