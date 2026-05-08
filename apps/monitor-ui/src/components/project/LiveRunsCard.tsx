/**
 * LiveRunsCard — compact floating card showing only the active runs
 * (running / paused / starting). Draggable to any position in the
 * canvas area; position is persisted to localStorage.
 *
 * Idle runs live in the full RunsDrawer behind the "all runs" button —
 * useful but not warranting permanent screen real estate.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { GripVertical } from "lucide-react";

import type { ThreadStatus, ThreadSummary } from "@/api";
import { useListThreadsQuery } from "@/api";
import { Badge } from "@/components/ui/badge";
import { deriveLabel } from "@/components/project/RunsSidebar";
import { StalenessBadge } from "@/components/project/StalenessBadge";
import { formatElapsed, getStaleness, STALENESS_PRIORITY, thresholdsFromRunning } from "@/lib/staleness";
import { cn } from "@/lib/utils";

const STATUS_DOT: Record<ThreadStatus, string> = {
  running: "bg-emerald-400 animate-pulse",
  paused: "bg-amber-400",
  starting: "bg-sky-400 animate-pulse",
  idle: "bg-zinc-500",
};

const POSITION_KEY = "chimera-monitor-liveruns-position";
const DEFAULT_POSITION = { x: 12, y: 12 };

interface Position {
  x: number;
  y: number;
}

function loadPosition(): Position {
  try {
    const raw = localStorage.getItem(POSITION_KEY);
    if (!raw) return DEFAULT_POSITION;
    const parsed = JSON.parse(raw);
    if (typeof parsed?.x === "number" && typeof parsed?.y === "number") {
      return { x: parsed.x, y: parsed.y };
    }
  } catch { /* ignore */ }
  return DEFAULT_POSITION;
}

function savePosition(p: Position) {
  try {
    localStorage.setItem(POSITION_KEY, JSON.stringify(p));
  } catch { /* ignore */ }
}

interface LiveRunsCardProps {
  projectName: string;
  selectedThreadId: string | null;
  onSelectThread: (threadId: string | null) => void;
  onOpenAllRuns: () => void;
}

export function LiveRunsCard({
  projectName,
  selectedThreadId,
  onSelectThread,
  onOpenAllRuns,
}: LiveRunsCardProps) {
  const { data } = useListThreadsQuery(
    { name: projectName, limit: 100, offset: 0 },
    { pollingInterval: 2000 },
  );

  const threads = data?.threads ?? [];
  const stalenessThresholds = thresholdsFromRunning(data?.running_threshold_seconds);
  // Sort: stuck first, then stale, then hitl-idle, then fresh — within
  // each tier, newest-updated first. Forces user attention to anything
  // that needs it without burying healthy active runs.
  const live = threads
    .filter((t) => t.status === "running" || t.status === "paused" || t.status === "starting")
    .sort((a, b) => {
      const da = STALENESS_PRIORITY[getStaleness(a, stalenessThresholds)];
      const db = STALENESS_PRIORITY[getStaleness(b, stalenessThresholds)];
      if (da !== db) return da - db;
      return (b.last_updated ?? "").localeCompare(a.last_updated ?? "");
    });

  const stuckCount = live.filter((t) => getStaleness(t, stalenessThresholds) === "stuck").length;
  const idleCount = threads.length - live.length;

  // Drag state ------------------------------------------------------------
  const [position, setPosition] = useState<Position>(DEFAULT_POSITION);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ startX: number; startY: number; origX: number; origY: number } | null>(null);

  useEffect(() => {
    setPosition(loadPosition());
  }, []);

  // When the parent (canvas area) resizes, clamp the card back inside
  // the new bounds. Without this the card stays at its persisted pixel
  // position and can end up off-screen after the user resizes the window.
  useEffect(() => {
    const card = cardRef.current;
    if (!card?.parentElement) return;
    const parent = card.parentElement;
    const observer = new ResizeObserver(() => {
      const parentRect = parent.getBoundingClientRect();
      const cardRect = card.getBoundingClientRect();
      const maxX = Math.max(4, parentRect.width - cardRect.width - 4);
      const maxY = Math.max(4, parentRect.height - cardRect.height - 4);
      setPosition((p) => {
        const clamped = { x: Math.min(p.x, maxX), y: Math.min(p.y, maxY) };
        if (clamped.x !== p.x || clamped.y !== p.y) {
          savePosition(clamped);
          return clamped;
        }
        return p;
      });
    });
    observer.observe(parent);
    return () => observer.disconnect();
  }, []);

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    // Only initiate drag from the header / handle — rows shouldn't drag.
    const target = e.target as HTMLElement;
    if (target.closest("[data-no-drag]")) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      origX: position.x,
      origY: position.y,
    };
  }, [position]);

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || !cardRef.current) return;
    const parent = cardRef.current.parentElement;
    if (!parent) return;
    const parentRect = parent.getBoundingClientRect();
    const cardRect = cardRef.current.getBoundingClientRect();
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    // Constrain to the canvas area so the card can't be dragged off-screen.
    const maxX = parentRect.width - cardRect.width - 4;
    const maxY = parentRect.height - cardRect.height - 4;
    const x = Math.max(4, Math.min(drag.origX + dx, maxX));
    const y = Math.max(4, Math.min(drag.origY + dy, maxY));
    setPosition({ x, y });
  }, []);

  const onPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    dragRef.current = null;
    setPosition((p) => {
      savePosition(p);
      return p;
    });
  }, []);

  return (
    <div
      ref={cardRef}
      className="absolute z-10 w-72 rounded-lg border border-border bg-card/95 backdrop-blur shadow-lg select-none"
      style={{ left: position.x, top: position.y }}
    >
      <div
        className="flex items-center justify-between gap-2 px-2 py-2 border-b border-border/60 cursor-grab active:cursor-grabbing"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        title="Drag to reposition"
      >
        <div className="flex items-center gap-1.5">
          <GripVertical className="h-3.5 w-3.5 text-muted-foreground/60" />
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground">live runs</span>
        </div>
        <div className="flex items-center gap-1" data-no-drag>
          {stuckCount > 0 ? (
            <span
              className="inline-flex items-center rounded border border-rose-500/50 bg-rose-500/15 px-1 text-[9px] font-mono text-rose-300 animate-pulse"
              title={`${stuckCount} thread${stuckCount === 1 ? "" : "s"} appear stuck (>15 min since last update)`}
            >
              {stuckCount} stuck
            </span>
          ) : null}
          <Badge variant="outline" className="text-[10px]">{live.length}</Badge>
        </div>
      </div>
      {live.length === 0 ? (
        <p className="px-3 py-3 text-[11px] text-muted-foreground italic" data-no-drag>
          no active runs
        </p>
      ) : (
        <ul className="max-h-64 overflow-auto" data-no-drag>
          {live.map((t) => (
            <LiveRunRow
              key={t.thread_id}
              thread={t}
              active={selectedThreadId === t.thread_id}
              stalenessThresholds={stalenessThresholds}
              onClick={() =>
                onSelectThread(selectedThreadId === t.thread_id ? null : t.thread_id)
              }
            />
          ))}
        </ul>
      )}
      <button
        type="button"
        onClick={onOpenAllRuns}
        data-no-drag
        className="block w-full border-t border-border/60 px-3 py-1.5 text-left text-[11px] text-muted-foreground hover:text-foreground hover:bg-accent/40 transition-colors"
      >
        all runs <span className="ml-1 text-muted-foreground/60">({threads.length} total · {idleCount} idle)</span>
      </button>
    </div>
  );
}

function LiveRunRow({
  thread,
  active,
  onClick,
  stalenessThresholds,
}: {
  thread: ThreadSummary;
  active: boolean;
  onClick: () => void;
  stalenessThresholds: ReturnType<typeof thresholdsFromRunning>;
}) {
  const label = deriveLabel(thread);
  const staleness = getStaleness(thread, stalenessThresholds);
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className={cn(
          "block w-full px-3 py-1.5 text-left text-xs hover:bg-accent/50 transition-colors",
          active && "bg-accent text-accent-foreground",
          // Subtle left-border tint to make stuck/stale rows pop out of
          // the live-runs list at a glance, even when scrolled.
          staleness === "stuck" && "border-l-2 border-l-rose-500",
          staleness === "stale" && "border-l-2 border-l-amber-500",
          staleness === "hitl-idle" && "border-l-2 border-l-amber-500/60",
        )}
      >
        <div className="flex items-center gap-1.5">
          <span className={cn("h-1.5 w-1.5 rounded-full shrink-0", STATUS_DOT[thread.status])} />
          <span className="font-medium truncate flex-1 min-w-0">{label.primary}</span>
          <StalenessBadge staleness={staleness} lastUpdated={thread.last_updated} />
        </div>
        <div className="mt-0.5 ml-3 flex items-center gap-1.5 text-[10px] text-muted-foreground">
          {thread.current_node ? (
            <span className="font-mono text-foreground/80 truncate">@{thread.current_node}</span>
          ) : null}
          {thread.step != null ? <span>· step {thread.step}</span> : null}
          {/* Time since last checkpoint — when the marker hasn't moved
              but seconds keep ticking, the next node is running (we
              just can't see it from the checkpoint table). */}
          <span
            className="ml-auto shrink-0 font-mono"
            title={`last checkpoint ${formatElapsed(thread.last_updated)} ago`}
          >
            {formatElapsed(thread.last_updated)} ago
          </span>
        </div>
      </button>
    </li>
  );
}
