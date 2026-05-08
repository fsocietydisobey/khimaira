import { useEffect, useState } from "react";

export type Theme = "dark" | "space-gray";

const THEME_KEY = "chimera-monitor-theme";
const THEMES: Theme[] = ["dark", "space-gray"];

export const THEME_LABELS: Record<Theme, string> = {
  "dark": "Dark",
  "space-gray": "Space Gray",
};

function loadTheme(): Theme {
  try {
    const raw = localStorage.getItem(THEME_KEY);
    if (raw && THEMES.includes(raw as Theme)) return raw as Theme;
  } catch { /* ignore */ }
  return "space-gray"; // softer default — easier on the eyes
}

function applyTheme(theme: Theme) {
  // Both classes drive a different palette via :root selectors in
  // globals.css. Removing the others keeps the cascade clean.
  const root = document.documentElement;
  for (const t of THEMES) root.classList.remove(t);
  root.classList.add(theme);
}

/**
 * Theme hook — returns [theme, setTheme]. Persists to localStorage and
 * applies the matching class to <html> so Tailwind's CSS variables
 * resolve to the right palette.
 */
export function useTheme(): [Theme, (next: Theme) => void] {
  const [theme, setThemeState] = useState<Theme>(() => loadTheme());

  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch { /* ignore */ }
  }, [theme]);

  return [theme, setThemeState];
}

export const ALL_THEMES: Theme[] = THEMES;
