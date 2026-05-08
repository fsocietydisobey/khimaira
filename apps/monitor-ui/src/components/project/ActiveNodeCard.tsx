/**
 * ActiveNodeCard — draggable floating card that mirrors whichever node
 * is currently "active" on the canvas. Visible when ghost mode is on,
 * since the canvas zooms out to show every fired node and individual
 * labels become hard to read.
 *
 * Source of "active":
 *   - in replay mode → the replay-active node (the step the scrubber sits on)
 *   - in live mode   → the focused thread's current_node
 *
 * Position is dragged by the user and persisted to localStorage so the
 * card stays where they parked it across renders/sessions.
 */

import { GripHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { formatElapsed } from "@/lib/staleness";
import { cn } from "@/lib/utils";

interface ActiveNodeCardProps {
  visible: boolean;
  graphLabel: string | null;
  nodeName: string | null;
  /** 1-based step number in the merged run timeline. Null if not known. */
  stepNumber: number | null;
  /** Total steps in the merged timeline. Null if not known. */
  totalSteps: number | null;
  /** True when the canvas is in replay mode (otherwise live). */
  inReplay: boolean;
  /** ISO of the last checkpoint write. Drives the "Ns ago" hint that
   *  signals the next node is running even when the marker hasn't
   *  advanced. Null in replay (where elapsed isn't meaningful). */
  lastUpdated: string | null;
}

const STORAGE_KEY = "chimera-monitor-active-node-card-pos";

function loadPos(): { x: number; y: number } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { x: 16, y: 80 };
    const parsed = JSON.parse(raw);
    if (typeof parsed?.x === "number" && typeof parsed?.y === "number") {
      return parsed;
    }
  } catch {
    /* ignore */
  }
  return { x: 16, y: 80 };
}

export function ActiveNodeCard({
  visible,
  graphLabel,
  nodeName,
  stepNumber,
  totalSteps,
  inReplay,
  lastUpdated,
}: ActiveNodeCardProps) {
  const [pos, setPos] = useState(loadPos);
  const dragRef = useRef<{ startX: number; startY: number; baseX: number; baseY: number } | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(pos));
    } catch {
      /* ignore */
    }
  }, [pos]);

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

  const displayNode = nodeName ?? "—";
  const noActive = !nodeName;

  return (
    <div
      className="absolute z-30 select-none rounded-lg border border-border bg-card/95 shadow-2xl backdrop-blur"
      style={{ left: pos.x, top: pos.y, width: 240 }}
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
          active node
        </span>
        <Badge
          variant={inReplay ? "warning" : "outline"}
          className="ml-auto text-[9px]"
        >
          {inReplay ? "replay" : "live"}
        </Badge>
      </div>
      <div className="px-3 py-2">
        <div
          className={cn(
            "font-mono text-sm break-all",
            noActive ? "text-muted-foreground italic" : "text-foreground",
          )}
        >
          {displayNode}
        </div>
        <div className="mt-1 flex items-center justify-between text-[10px] text-muted-foreground">
          <span className="truncate" title={graphLabel ?? ""}>
            {graphLabel ?? "—"}
          </span>
          {stepNumber != null && totalSteps != null ? (
            <span className="font-mono shrink-0 ml-2">
              step {stepNumber}/{totalSteps}
            </span>
          ) : null}
        </div>
        {!inReplay && lastUpdated ? (
          <div
            className="mt-1 text-[10px] text-muted-foreground/70 font-mono"
            title="Time since the last checkpoint write. If this number keeps growing while the node label doesn't move, the next node is running internally — LangGraph just hasn't committed a new checkpoint yet."
          >
            last checkpoint {formatElapsed(lastUpdated)} ago
          </div>
        ) : null}
      </div>
    </div>
  );
}
