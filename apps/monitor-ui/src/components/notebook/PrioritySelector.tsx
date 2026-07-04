/**
 * PrioritySelector — a colored-dot priority selector for notes + study
 * guides (Grimoire, tasks/grimoire/SENSITIVE-AND-FLAGS.md Feature B).
 * `priority` is a user-set importance dimension, independent of `status`
 * (lifecycle) — same badge-column region as IdChip + the status badge.
 *
 * A native `<select>` (same precedent as the collections-filter dropdown
 * and the theme picker) rather than a custom widget — the colored dot is
 * just an emoji prefix on each option's label, so the closed state already
 * shows "🔴 urgent" etc. without a separate badge element.
 */

import type { NotePriority } from "@/components/notebook/notebookTypes";
import { cn } from "@/lib/utils";

const PRIORITY_ORDER: NotePriority[] = ["urgent", "high", "normal", "low"];

const PRIORITY_DOT: Record<NotePriority, string> = {
  urgent: "🔴",
  high: "🟠",
  normal: "⚪",
  low: "⚫",
};

export function priorityDot(priority: NotePriority): string {
  return PRIORITY_DOT[priority];
}

export function PrioritySelector({
  priority,
  onChange,
  className,
}: {
  priority: NotePriority;
  onChange: (p: NotePriority) => void;
  className?: string;
}) {
  return (
    <select
      value={priority}
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => onChange(e.target.value as NotePriority)}
      title="Priority"
      className={cn(
        "h-5 shrink-0 rounded border border-input bg-background px-1 text-[9px] text-muted-foreground outline-none hover:text-foreground focus:ring-1 focus:ring-ring",
        className,
      )}
    >
      {PRIORITY_ORDER.map((p) => (
        <option key={p} value={p}>
          {PRIORITY_DOT[p]} {p}
        </option>
      ))}
    </select>
  );
}
