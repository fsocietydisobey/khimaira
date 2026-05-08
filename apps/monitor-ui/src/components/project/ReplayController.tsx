/**
 * ReplayController — scrub through any focused thread's checkpoint
 * history, lighting up the node that was active at each step.
 *
 * Renders as a fixed overlay at the bottom of the canvas (above the
 * minimap) when a thread is focused. The user controls playback;
 * the canvas reads `currentReplayNode` and lights it up like a live
 * active node.
 *
 * For live runs, "live" mode (the default) follows the polling stream.
 * Hitting play or scrubbing backward enters "replay" mode — the canvas
 * shows historical state until the user clicks the live indicator to
 * snap back.
 *
 * Checkpoints come in via the `checkpoints` prop — fetching is owned by
 * the parent (ProjectView) so it can also feed the ghost overlay's
 * fired-nodes map without duplicating network calls.
 */

import { Eye, EyeOff, Pause, Play, SkipBack, SkipForward, Square } from "lucide-react";
import { useEffect, useRef } from "react";

import type { CheckpointDetail } from "@/api";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export interface ReplayState {
  // Index into the chronologically-ordered checkpoints (0 = oldest).
  // null = follow live (no replay override).
  index: number | null;
  playing: boolean;
  speedMs: number; // ms per step
}

interface ReplayControllerProps {
  threadIsLive: boolean; // running/paused/starting → "live" anchor available
  state: ReplayState;
  onState: (next: ReplayState) => void;
  onActiveNodeChange: (node: string | null) => void;
  /**
   * Pre-merged chronological checkpoint list for the run (focused
   * thread + sibling threads, oldest first). Owned by parent so the
   * ghost overlay can consume the same list.
   */
  checkpoints: CheckpointDetail[];
  /** Loading flag from the upstream fetch. */
  isLoading: boolean;
  /** When > 0, the timeline crosses multiple sister threads. */
  siblingCount: number;
  /** Identity of the currently-focused thread — used as effect key. */
  threadId: string;
  /** Current ghost-mode flag. */
  ghostMode: boolean;
  onToggleGhost: () => void;
}

const SPEEDS = [
  { label: "0.25×", ms: 3000 },
  { label: "0.5×", ms: 1500 },
  { label: "1×", ms: 750 },
  { label: "2×", ms: 350 },
  { label: "5×", ms: 120 },
];


export function ReplayController({
  threadIsLive,
  state,
  onState,
  onActiveNodeChange,
  checkpoints,
  isLoading,
  siblingCount,
  threadId,
  ghostMode,
  onToggleGhost,
}: ReplayControllerProps) {
  const total = checkpoints.length;
  const liveIndex = total - 1;
  const isRunMode = siblingCount > 0;

  // Derive what node to show as active. In live mode, parent passes
  // the live thread's current_node directly (we set null here so the
  // override is "no override"). In replay mode, light up the checkpoint
  // node at `state.index`.
  useEffect(() => {
    if (state.index === null) {
      onActiveNodeChange(null);
      return;
    }
    if (state.index < 0 || state.index >= total) return;
    const node = checkpoints[state.index]?.node ?? null;
    onActiveNodeChange(node);
  }, [state.index, checkpoints, onActiveNodeChange, total]);

  // Auto-play tick — advance the index on a timer when playing.
  // Gate on total > 0 so we don't auto-pause when checkpoints haven't
  // finished loading yet (the play-run handler sets playing:true
  // immediately; the checkpoint fetch may take 100-500ms and during
  // that window total=0).
  const tickRef = useRef<number | null>(null);
  useEffect(() => {
    if (!state.playing || state.index === null || total === 0) {
      if (tickRef.current !== null) {
        window.clearInterval(tickRef.current);
        tickRef.current = null;
      }
      return;
    }
    tickRef.current = window.setInterval(() => {
      onState({
        ...state,
        index: state.index === null
          ? null
          : state.index + 1 >= total
            ? state.index // hold at end
            : state.index + 1,
        // Auto-pause when we hit the end.
        playing: state.index === null ? state.playing : state.index + 1 < total,
      });
    }, state.speedMs);
    return () => {
      if (tickRef.current !== null) window.clearInterval(tickRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.playing, state.index, state.speedMs, total]);

  // When the thread changes, reset to live (or first if no live anchor).
  useEffect(() => {
    onState({
      index: threadIsLive ? null : 0,
      playing: false,
      speedMs: state.speedMs,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, threadIsLive]);

  if (isLoading || total === 0) {
    return (
      <div className="absolute bottom-3 left-1/2 z-20 -translate-x-1/2 rounded-lg border border-border bg-card/95 px-3 py-2 shadow-lg backdrop-blur">
        <span className="text-[11px] text-muted-foreground">
          {isLoading ? "loading checkpoints…" : "no checkpoints to replay"}
        </span>
      </div>
    );
  }

  const inReplay = state.index !== null;
  const displayedIndex = state.index ?? liveIndex;
  const cp = checkpoints[displayedIndex];

  const setIndex = (i: number) => {
    const clamped = Math.max(0, Math.min(i, total - 1));
    onState({ ...state, index: clamped });
  };

  const goLive = () => onState({ ...state, index: null, playing: false });
  const togglePlay = () => {
    if (state.index === null) {
      // Entering replay from live: start at 0 so play has somewhere to go.
      onState({ ...state, index: 0, playing: true });
    } else {
      onState({ ...state, playing: !state.playing });
    }
  };

  return (
    <div className="absolute bottom-3 left-1/2 z-20 w-[min(680px,calc(100%-2rem))] -translate-x-1/2 rounded-lg border border-border bg-card/95 shadow-lg backdrop-blur">
      <div className="flex items-center gap-3 border-b border-border/60 px-3 py-1.5">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {isRunMode ? `replay · run (${1 + siblingCount} threads)` : "replay"}
        </span>
        {inReplay ? (
          <Badge variant="warning" className="text-[10px]">
            replay · step {displayedIndex + 1}/{total}
          </Badge>
        ) : (
          <Badge variant="outline" className="text-[10px]">
            live · step {displayedIndex + 1}/{total}
          </Badge>
        )}
        {cp ? (
          <span className="text-[11px] text-muted-foreground font-mono truncate">
            {cp.node ?? "—"} · {cp.created_at?.slice(11, 19) ?? ""}
          </span>
        ) : null}
        {threadIsLive ? (
          <button
            type="button"
            onClick={goLive}
            className={cn(
              "ml-auto text-[10px] hover:text-foreground",
              inReplay ? "text-emerald-300" : "text-muted-foreground/60",
            )}
            disabled={!inReplay}
          >
            ● back to live
          </button>
        ) : null}
      </div>

      <div className="flex items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setIndex((state.index ?? liveIndex) - 1)}
          className="rounded p-1 hover:bg-accent"
          aria-label="step back"
        >
          <SkipBack className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={togglePlay}
          className="rounded p-1 hover:bg-accent"
          aria-label={state.playing ? "pause" : "play"}
        >
          {state.playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
        </button>
        <button
          type="button"
          onClick={() => setIndex((state.index ?? liveIndex) + 1)}
          className="rounded p-1 hover:bg-accent"
          aria-label="step forward"
        >
          <SkipForward className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => onState({ ...state, index: 0, playing: false })}
          className="rounded p-1 hover:bg-accent"
          aria-label="reset to start"
          title="back to start"
        >
          <Square className="h-3 w-3" />
        </button>

        <input
          type="range"
          min={0}
          max={total - 1}
          value={displayedIndex}
          onChange={(e) => setIndex(Number(e.target.value))}
          className="flex-1 accent-emerald-500"
        />

        <select
          value={state.speedMs}
          onChange={(e) => onState({ ...state, speedMs: Number(e.target.value) })}
          className="rounded border border-input bg-background px-1 py-0.5 text-[11px]"
        >
          {SPEEDS.map((s) => (
            <option key={s.ms} value={s.ms}>
              {s.label}
            </option>
          ))}
        </select>
      </div>

      {/* Toolbar — grows over time. First tool: ghost overlay toggle. */}
      <div className="flex items-center gap-1 border-t border-border/60 px-2 py-1">
        <span className="text-[9px] uppercase tracking-wider text-muted-foreground/70 px-1">
          tools
        </span>
        <button
          type="button"
          onClick={onToggleGhost}
          aria-pressed={ghostMode}
          title={
            ghostMode
              ? "Hide the ghost overlay (numbered fired nodes)"
              : "Show ghost overlay — every node that fired in this run is numbered in execution order"
          }
          className={cn(
            "inline-flex h-6 items-center gap-1 rounded px-1.5 text-[10px] transition-colors",
            ghostMode
              ? "bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25"
              : "text-muted-foreground hover:bg-accent hover:text-foreground",
          )}
        >
          {ghostMode ? <Eye className="h-3 w-3" /> : <EyeOff className="h-3 w-3" />}
          ghost
        </button>
      </div>
    </div>
  );
}
