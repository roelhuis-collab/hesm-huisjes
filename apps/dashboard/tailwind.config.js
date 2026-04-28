/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        // Default Tailwind sans is system-ui — works fine. We override mono
        // to a single font so tabular-nums look consistent across platforms.
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      colors: {
        // Project accent palette — single warm amber on dark slate.
        // Matches the artifact preview Roel approved during scoping.
        accent: {
          DEFAULT: '#fbbf24',  // amber-400
          warm: '#f59e0b',     // amber-500
        },
      },
    },
  },
  plugins: [],
};
