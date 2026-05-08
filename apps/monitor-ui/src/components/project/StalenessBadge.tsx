/**
 * StalenessBadge — small inline label rendered next to a thread row
 * when the thread has stalled. Renders nothing for fresh threads.
 *
 * Treatment by tier:
 *   stale     — amber chip, "stale Nm" (so user knows how stale)
 *   stuck     — red chip, "stuck Nm", subtle pulse to draw the eye
 *   hitl-idle — amber outline chip, "HITL idle Nm", less urgent
 */

import type { Staleness } from "@/lib/staleness";
import { cn } from "@/lib/utils";

interface StalenessBadgeProps {
  staleness: Staleness;
  /** ISO last_updated to compute the elapsed-minutes label. */
  lastUpdated: string | null;
  className?: string;
}

function elapsedMinutesLabel(lastUpdated: string | null): string {
  if (!lastUpdated) return "";
  const t = new Date(lastUpdated).getTime();
  if (!Number.isFinite(t)) return "";
  const minutes = Math.max(0, Math.floor((Date.now() - t) / 60_000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export function StalenessBadge({ staleness, lastUpdated, className }: StalenessBadgeProps) {
  if (staleness === "fresh") return null;
  const elapsed = elapsedMinutesLabel(lastUpdated);
  const baseLabel = staleness === "stuck" ? "stuck"
    : staleness === "stale" ? "stale"
    : "HITL idle";
  const label = elapsed ? `${baseLabel} ${elapsed}` : baseLabel;

  const tone = {
    stuck: "border-rose-500/50 bg-rose-500/15 text-rose-300 animate-pulse",
    stale: "border-amber-500/50 bg-amber-500/15 text-amber-300",
    "hitl-idle": "border-amber-500/40 bg-transparent text-amber-300/90",
  }[staleness];

  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1 text-[9px] font-mono shrink-0",
        tone,
        className,
      )}
    >
      {label}
    </span>
  );
}
