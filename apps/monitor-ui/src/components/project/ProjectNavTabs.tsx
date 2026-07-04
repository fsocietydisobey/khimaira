/**
 * ProjectNavTabs — horizontal icon nav for switching between project views.
 *
 * Rendered at the top of ProjectView, CostDashboard, TraceWaterfall so the
 * user can hop between observability surfaces for the same project without
 * going back to the projects index.
 *
 * Active tab: matched against current pathname. The trace tab is enabled
 * only when a correlation_id is available (otherwise it'd 404 visually).
 */

import { Activity, GitFork, Network, NotebookText, Receipt } from "lucide-react";
import { NavLink } from "react-router-dom";

import { cn } from "@/lib/utils";


interface NavItem {
  to: string;
  label: string;
  icon: typeof GitFork;
  title: string;
  /** When true, render disabled (no link) — for views needing extra params. */
  disabled?: boolean;
}


export function ProjectNavTabs({
  projectName,
  currentCorrelationId,
}: {
  projectName: string;
  /** When provided, the trace tab links to this run; otherwise disabled. */
  currentCorrelationId?: string;
}) {
  const items: NavItem[] = [
    {
      to: `/${projectName}`,
      label: "topology",
      icon: GitFork,
      title: "Live LangGraph topology + run replay",
    },
    {
      to: `/${projectName}/cost`,
      label: "cost",
      icon: Receipt,
      title: "Estimated USD spend + token usage by model",
    },
    {
      to: currentCorrelationId
        ? `/${projectName}/trace/${currentCorrelationId}`
        : "#",
      label: "trace",
      icon: Activity,
      title: currentCorrelationId
        ? "Trace waterfall — chain/llm/tool/external timeline for the current run"
        : "Trace waterfall — needs a correlation_id (set via khimaira_observer.tag_run() in your app)",
      disabled: !currentCorrelationId,
    },
    {
      to: `/${projectName}/kg`,
      label: "kg",
      icon: Network,
      title: "Knowledge graph — interactive node-edge view of the jeevy KG for a deliverable",
    },
    {
      // Route/path stays "notebook" (internal namespace) — only the
      // user-visible label is "Grimoire" (Joseph's product name, 2026-07-04).
      to: `/${projectName}/notebook`,
      label: "grimoire",
      icon: NotebookText,
      title: "Grimoire — paste + auto-structure notes, house study guides, curate into the mnemosyne knowledge loop",
    },
  ];

  return (
    <nav className="flex items-center gap-0.5 text-[11px]">
      {items.map((item) => {
        const Icon = item.icon;
        if (item.disabled) {
          return (
            <span
              key={item.label}
              className={cn(
                "inline-flex items-center gap-1.5 px-2 py-1 rounded-md",
                "text-muted-foreground/40 cursor-not-allowed",
              )}
              title={item.title}
            >
              <Icon className="h-3.5 w-3.5" />
              {item.label}
            </span>
          );
        }
        return (
          <NavLink
            key={item.label}
            to={item.to}
            end={item.label === "topology"}
            title={item.title}
            className={({ isActive }) =>
              cn(
                "inline-flex items-center gap-1.5 px-2 py-1 rounded-md transition-colors",
                isActive
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
              )
            }
          >
            <Icon className="h-3.5 w-3.5" />
            {item.label}
          </NavLink>
        );
      })}
    </nav>
  );
}
