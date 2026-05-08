/**
 * MonitorShell — header + project sidebar + content area.
 *
 * Ported from jeevy_portal/frontend/src/features/ai-debugger/AIDebuggerShell.js
 * (re-port at every chimera-monitor phase boundary per locked decision 2026-05-06).
 *
 * Diverges from jeevy's shellRegistry pattern in favor of React Router —
 * chimera-monitor's navigation is project × view (e.g. /chimera/topology),
 * not a single global activeViewId. Sidebar navigation is anchored on the
 * project; per-view tabs live inside the project layout.
 */

import { Boxes, FolderGit2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useParams } from "react-router-dom";

import { useListProjectsQuery } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ALL_THEMES, THEME_LABELS, useTheme, type Theme } from "@/lib/theme";
import { cn } from "@/lib/utils";

const SIDEBAR_COLLAPSED_KEY = "chimera-monitor-sidebar-collapsed";

const loadCollapsed = (): boolean => {
  try {
    return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "true";
  } catch {
    return false;
  }
};

const saveCollapsed = (value: boolean): void => {
  try {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(value));
  } catch {
    /* ignore */
  }
};

export function MonitorShell() {
  const [collapsed, setCollapsed] = useState(false);
  const { data: projects } = useListProjectsQuery();
  const { name } = useParams<{ name: string }>();

  useEffect(() => {
    setCollapsed(loadCollapsed());
  }, []);

  const toggleCollapsed = () => {
    setCollapsed((prev) => {
      const next = !prev;
      saveCollapsed(next);
      return next;
    });
  };

  // Theme is initialized inside the picker via useTheme; this hook
  // call ensures the theme class is applied on first render even if
  // the user never opens the picker.
  useTheme();

  return (
    <div className="flex h-screen min-h-0 flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-card px-4 min-w-0 gap-3">
        <Link to="/" className="flex items-center gap-2 min-w-0">
          <span className="text-sm font-semibold truncate">CHIMERA</span>
          <Badge variant="secondary" className="font-mono text-[10px] shrink-0">
            monitor
          </Badge>
        </Link>
        <div className="flex items-center gap-2 shrink-0">
          <ThemePicker />
          <Badge variant="outline" className="font-mono text-[10px] shrink-0">
            127.0.0.1
          </Badge>
        </div>
      </header>

      <div className="flex flex-1 min-h-0 min-w-0 overflow-hidden">
        <aside
          className={cn(
            "flex shrink-0 flex-col border-r border-border bg-card transition-[width] duration-150",
            collapsed ? "w-12" : "w-60",
          )}
        >
          <Button
            variant="ghost"
            size="sm"
            onClick={toggleCollapsed}
            className="m-1 self-end"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? "›" : "‹"}
          </Button>

          <div className={cn("pb-2", collapsed ? "px-1" : "px-2")}>
            {!collapsed ? (
              <p className="px-2 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                Projects
              </p>
            ) : null}
            <nav className="flex flex-col gap-0.5">
              {(projects ?? []).map((p) => (
                <NavLink
                  key={p.name}
                  to={`/${p.name}`}
                  title={p.name}
                  className={({ isActive }) =>
                    cn(
                      "rounded-md text-sm transition-colors hover:bg-accent flex items-center gap-2",
                      collapsed
                        ? "h-9 w-9 justify-center mx-auto"
                        : "px-2 py-1.5",
                      isActive || name === p.name
                        ? "bg-accent text-accent-foreground"
                        : "text-foreground/80",
                    )
                  }
                >
                  <FolderGit2 className={cn("shrink-0", collapsed ? "h-4 w-4" : "h-3.5 w-3.5 text-muted-foreground")} />
                  {!collapsed ? <span className="truncate">{p.name}</span> : null}
                </NavLink>
              ))}
              {(projects ?? []).length === 0 ? (
                collapsed ? (
                  <div className="flex items-center justify-center text-muted-foreground/60 h-9" title="no projects discovered">
                    <Boxes className="h-4 w-4" />
                  </div>
                ) : (
                  <p className="px-2 py-1 text-xs text-muted-foreground">no projects discovered</p>
                )
              ) : null}
            </nav>
          </div>
        </aside>

        <main className="flex-1 min-w-0 min-h-0 overflow-hidden">
          <Outlet />
        </main>
      </div>
    </div>
  );
}


function ThemePicker() {
  const [theme, setTheme] = useTheme();
  return (
    <select
      value={theme}
      onChange={(e) => setTheme(e.target.value as Theme)}
      className="h-7 rounded-md border border-input bg-background px-2 text-[11px] text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
      title="Switch palette — 'Space Gray' is softer than the default 'Dark'"
    >
      {ALL_THEMES.map((t) => (
        <option key={t} value={t}>{THEME_LABELS[t]}</option>
      ))}
    </select>
  );
}
