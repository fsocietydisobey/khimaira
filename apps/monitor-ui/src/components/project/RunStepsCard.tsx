/**
 * RunStepsCard — draggable list of every step in the focused run, in
 * chronological order. Visible whenever ghost mode is on (paired with
 * ActiveNodeCard so the user has both "what's lit now" and "the whole
 * sequence" at once).
 *
 * Behavior:
 *   - Each row shows step number, node name, graph label, and timestamp.
 *   - Clicking a row jumps the replay scrubber to that step (and pauses).
 *   - The row matching the current replay index is highlighted and
 *     auto-scrolled into view; if play is ticking, the list follows.
 *
 * Position is dragged by the user and persisted to localStorage.
 */

import { GripHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { CheckpointDetail } from "@/api";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface RunStepsCardProps {
  visible: boolean;
  checkpoints: CheckpointDetail[];
  /** Currently-active step index (0-based). Null in pure live mode. */
  activeIndex: number | null;
  /** Resolves a checkpoint's source thread_id → graph label. */
  resolveGraphLabel: (threadId: string) => string | null;
  onSelectStep: (index: number) => void;
}

const STORAGE_KEY = "chimera-monitor-run-steps-card-pos";

interface CheckpointWithThread extends CheckpointDetail {
  thread_id: string;
}

function loadPos(): { x: number; y: number } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { x: 16, y: 200 };
    const parsed = JSON.parse(raw);
    if (typeof parsed?.x === "number" && typeof parsed?.y === "number") {
      return parsed;
    }
  } catch {
    /* ignore */
  }
  return { x: 16, y: 200 };
}

export function RunStepsCard({
  visible,
  checkpoints,
  activeIndex,
  resolveGraphLabel,
  onSelectStep,
}: RunStepsCardProps) {
  const [pos, setPos] = useState(loadPos);
  const dragRef = useRef<{ startX: number; startY: number; baseX: number; baseY: number } | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const activeRowRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(pos));
    } catch {
      /* ignore */
    }
  }, [pos]);

  // Auto-scroll the active row into view as replay advances. `nearest`
  // keeps the list from jumping when the row is already visible.
  useEffect(() => {
    if (!visible || activeIndex === null) return;
    activeRowRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [visible, activeIndex]);

  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      baseX: pos.x,
      baseY: pos.y,
    };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const drag = dragRef.current;
    if (!drag) return;
    setPos({
      x: Math.max(4, drag.baseX + (e.clientX - drag.startX)),
      y: Math.max(4, drag.baseY + (e.clientY - drag.startY)),
    });
  };
  const onPointerUp = (e: React.PointerEvent) => {
    (e.target as HTMLElement).releasePointerCapture(e.pointerId);
    dragRef.current = null;
  };

  if (!visible) return null;

  return (
    <div
      className="absolute z-30 select-none rounded-lg border border-border bg-card/95 shadow-2xl backdrop-blur"
      style={{ left: pos.x, top: pos.y, width: 280, maxHeight: "60vh" }}
    >
      <div
        className="flex cursor-grab items-center gap-1.5 border-b border-border/60 px-2 py-1 active:cursor-grabbing"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <GripHorizontal className="h-3 w-3 text-muted-foreground" />
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
          run steps
        </span>
        <Badge variant="outline" className="ml-auto text-[9px]">
          {checkpoints.length}
        </Badge>
      </div>
      <div
        ref={listRef}
        className="overflow-y-auto"
        style={{ maxHeight: "calc(60vh - 32px)" }}
      >
        {checkpoints.length === 0 ? (
          <p className="px-3 py-2 text-[11px] text-muted-foreground italic">
            no steps yet
          </p>
        ) : (
          <ul className="divide-y divide-border/40">
            {checkpoints.map((cp, i) => {
              const isActive = i === activeIndex;
              const threadId = (cp as CheckpointWithThread).thread_id ?? "";
              const graphLabel = resolveGraphLabel(threadId);
              return (
                <li key={cp.checkpoint_id}>
                  <button
                    ref={isActive ? activeRowRef : undefined}
                    type="button"
                    onClick={() => onSelectStep(i)}
                    className={cn(
                      "block w-full px-2 py-1 text-left text-[11px] hover:bg-accent/40 transition-colors",
                      isActive && "bg-emerald-500/15 hover:bg-emerald-500/25",
                    )}
                  >
                    <div className="flex items-center gap-1.5">
                      <span
                        className={cn(
                          "inline-flex h-4 min-w-[1.25rem] items-center justify-center rounded-full px-1 text-[9px] font-bold shrink-0",
                          isActive
                            ? "bg-emerald-500 text-emerald-950"
                            : "bg-muted text-muted-foreground",
                        )}
                      >
                        {i + 1}
                      </span>
                      <span className={cn(
                        "font-mono truncate flex-1 min-w-0",
                        isActive ? "text-foreground" : "text-foreground/85",
                      )}>
                        {cp.node ?? "—"}
                      </span>
                      <span className="text-[9px] text-muted-foreground/70 shrink-0">
                        {cp.created_at?.slice(11, 19) ?? ""}
                      </span>
                    </div>
                    {graphLabel ? (
                      <div className="ml-6 mt-0.5 truncate text-[9px] text-muted-foreground/80">
                        {graphLabel}
                      </div>
                    ) : null}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
