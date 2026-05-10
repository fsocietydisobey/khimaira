/**
 * TraceWaterfall — visualize one app-level run's full event timeline.
 *
 * Backed by GET /api/heartbeats/{project}/by-correlation/{cid}. Renders
 * paired start/end events as horizontal bars on a time axis, color-coded
 * by kind (chain/llm/tool/external). Stacked vertically by start time so
 * parallel calls visibly burst (e.g. asyncio.gather of 3 Roboflow calls
 * appears as 3 bars starting within ~10ms of each other — the exact
 * Phase A pattern that motivated #58 + #6 in the first place).
 *
 * Layout:
 *   ┌───────────────────────────────────────────────────────────┐
 *   │ summary header (count, duration, kinds breakdown)         │
 *   ├───────────────────────────────────────────────────────────┤
 *   │ time axis (relative ms from t0)                           │
 *   │   ▓▓▓▓▓▓▓▓ chain run_node                  450ms          │
 *   │      ▓▓▓ llm gemini-2.5-flash               180ms         │
 *   │      ▓▓▓ llm gemini-2.5-flash               180ms         │  ← parallel
 *   │      ▓▓▓ llm gemini-2.5-flash               180ms         │
 *   │              ▓▓ tool ...                     50ms         │
 *   └───────────────────────────────────────────────────────────┘
 *
 * Reads `correlationId` from URL (/:name/trace/:correlationId).
 *
 * Goals:
 *  - Visual proof of parallelism (or lack thereof) for one run
 *  - Spot the long tail at a glance (single bar dominates the canvas)
 *  - Drill-down via tooltip → host/path/status/error fields
 */

import { useMemo } from "react";
import { useParams } from "react-router-dom";

import { useGetEventsByCorrelationQuery, type ObserverEvent } from "@/api";
import { ProjectNavTabs } from "@/components/project/ProjectNavTabs";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";


type CallKind = "chain" | "llm" | "tool" | "external";

interface CallSpan {
  kind: CallKind;
  name: string;
  startMs: number;        // relative to run start (t0), ms
  endMs: number | null;   // null if in-flight
  durationMs: number;
  runId: string | null;
  extra: Record<string, unknown> | null;
  error: string | null;
}

const KIND_COLORS: Record<CallKind, string> = {
  chain: "bg-sky-500/70 hover:bg-sky-400 border-sky-400",
  llm: "bg-violet-500/70 hover:bg-violet-400 border-violet-400",
  tool: "bg-emerald-500/70 hover:bg-emerald-400 border-emerald-400",
  external: "bg-amber-500/70 hover:bg-amber-400 border-amber-400",
};

const KIND_TEXT_COLORS: Record<CallKind, string> = {
  chain: "text-sky-300",
  llm: "text-violet-300",
  tool: "text-emerald-300",
  external: "text-amber-300",
};


function pairEvents(events: ObserverEvent[]): {
  spans: CallSpan[];
  t0: number;
  totalDurationMs: number;
} {
  if (events.length === 0) {
    return { spans: [], t0: 0, totalDurationMs: 0 };
  }
  // Sort by ts; lowest = t0
  const sorted = [...events].sort((a, b) => a.ts - b.ts);
  const t0 = sorted[0].ts;
  const tMax = sorted[sorted.length - 1].ts;

  // Pair start/end by (kind, run_id). Same key as backend's find_slow_calls.
  const starts = new Map<string, ObserverEvent>();
  const ends = new Map<string, { ev: ObserverEvent; isError: boolean }>();
  for (const ev of sorted) {
    const ek = ev.event;
    for (const k of ["chain", "llm", "tool", "external"] as CallKind[]) {
      if (!ek.startsWith(k)) continue;
      const suffix = ek.slice(k.length + 1);
      const key = `${k}|${ev.run_id || ""}`;
      if (suffix === "start") starts.set(key, ev);
      else if (suffix === "end") ends.set(key, { ev, isError: false });
      else if (suffix === "error") ends.set(key, { ev, isError: true });
      break;
    }
  }

  const spans: CallSpan[] = [];
  for (const [key, startEv] of starts.entries()) {
    const kind = key.split("|", 1)[0] as CallKind;
    const endRec = ends.get(key);
    const startMs = (startEv.ts - t0) * 1000;
    const endMs = endRec ? (endRec.ev.ts - t0) * 1000 : null;
    const durationMs = endMs !== null ? endMs - startMs : (tMax - startEv.ts) * 1000;
    spans.push({
      kind,
      name: startEv.name || endRec?.ev.name || "?",
      startMs,
      endMs,
      durationMs,
      runId: startEv.run_id,
      extra: endRec?.ev.extra ?? startEv.extra ?? null,
      error: endRec?.isError ? String((endRec.ev.extra as { error?: string })?.error ?? "errored") : null,
    });
  }

  // Sort spans by start time, ties by duration desc (so larger bars stack
  // first within a burst of parallel starts — easier to scan).
  spans.sort((a, b) => a.startMs - b.startMs || b.durationMs - a.durationMs);

  return {
    spans,
    t0,
    totalDurationMs: (tMax - t0) * 1000,
  };
}


export function TraceWaterfall() {
  const { name, correlationId } = useParams<{ name: string; correlationId: string }>();
  const projectName = name ?? "";
  const cid = correlationId ?? "";
  const { data, isLoading, error } = useGetEventsByCorrelationQuery(
    { project: projectName, correlationId: cid },
    {
      pollingInterval: 2000,
      skip: !projectName || !cid,
    },
  );

  const { spans, totalDurationMs } = useMemo(
    () => pairEvents(data?.events ?? []),
    [data],
  );

  const kindCounts = useMemo(() => {
    const out: Record<CallKind, number> = { chain: 0, llm: 0, tool: 0, external: 0 };
    for (const s of spans) out[s.kind] += 1;
    return out;
  }, [spans]);

  if (!projectName || !cid) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        missing project or correlation_id in URL. Expected
        <code>/&lt;project&gt;/trace/&lt;correlation_id&gt;</code>
      </div>
    );
  }
  if (isLoading) {
    return <div className="p-6 text-sm text-muted-foreground">loading trace…</div>;
  }
  if (error) {
    return (
      <div className="p-6 text-sm text-destructive">
        trace fetch failed: {String((error as { error?: string }).error ?? error)}
      </div>
    );
  }
  if (!data || data.event_count === 0) {
    return (
      <div className="flex h-full flex-col">
        <Header projectName={projectName} cid={cid} />
        <div className="p-6 text-sm text-muted-foreground">
          No events tagged with correlation_id <code>{cid}</code> found in{" "}
          <code>{projectName}</code>'s heartbeat buffer. Either:
          <ul className="mt-2 list-disc pl-5 space-y-1">
            <li>The app didn't wrap its run in <code>chimera_observer.tag_run(cid)</code></li>
            <li>The run completed &gt; 1h ago (heartbeat buffer TTL)</li>
            <li>The observer isn't attached to the project</li>
          </ul>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <Header
        projectName={projectName}
        cid={cid}
        eventCount={data.event_count}
        spanCount={spans.length}
        totalDurationMs={totalDurationMs}
        kindCounts={kindCounts}
      />
      <div className="flex-1 overflow-auto p-4">
        <Card>
          <CardContent className="p-4">
            {spans.length === 0 ? (
              <div className="text-sm text-muted-foreground">
                {data.event_count} raw events but no pair-able start/end calls
                — likely all in-flight or all end-only. Refresh shortly.
              </div>
            ) : (
              <Waterfall spans={spans} totalDurationMs={totalDurationMs} />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}


function Header({
  projectName,
  cid,
  eventCount,
  spanCount,
  totalDurationMs,
  kindCounts,
}: {
  projectName: string;
  cid: string;
  eventCount?: number;
  spanCount?: number;
  totalDurationMs?: number;
  kindCounts?: Record<CallKind, number>;
}) {
  return (
    <header className="shrink-0 border-b border-border bg-card/40 px-4 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">trace · {projectName}</h2>
          <p className="text-[11px] text-muted-foreground mt-0.5 font-mono break-all">
            correlation_id: {cid}
          </p>
        </div>
        <ProjectNavTabs projectName={projectName} currentCorrelationId={cid} />
        {totalDurationMs !== undefined ? (
          <div className="text-right text-[11px] text-muted-foreground space-y-0.5">
            <div>
              <span className="text-foreground font-mono">
                {formatDuration(totalDurationMs)}
              </span>{" "}
              total wall · {spanCount} call{spanCount === 1 ? "" : "s"} · {eventCount} events
            </div>
            {kindCounts ? (
              <div className="flex justify-end gap-2 text-[10px]">
                {(["chain", "llm", "tool", "external"] as CallKind[]).map((k) =>
                  kindCounts[k] > 0 ? (
                    <span key={k} className={cn("inline-flex items-center gap-1", KIND_TEXT_COLORS[k])}>
                      <span className={cn("inline-block w-2 h-2 rounded-sm", KIND_COLORS[k].split(" ")[0])} />
                      {kindCounts[k]} {k}
                    </span>
                  ) : null,
                )}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </header>
  );
}


function Waterfall({
  spans,
  totalDurationMs,
}: {
  spans: CallSpan[];
  totalDurationMs: number;
}) {
  // Each row is one span. Bar X = startMs%, width = duration%. Min width
  // 0.5% so very short calls stay visible.
  const totalForScale = Math.max(totalDurationMs, 1);

  return (
    <div className="space-y-0.5 font-mono text-[10px]">
      <TimeAxis totalDurationMs={totalForScale} />
      {spans.map((s, i) => {
        const leftPct = (s.startMs / totalForScale) * 100;
        const widthPct = Math.max((s.durationMs / totalForScale) * 100, 0.5);
        const inFlight = s.endMs === null;
        return (
          <div
            key={`${s.kind}-${s.runId}-${i}`}
            className="group relative grid grid-cols-[120px_1fr_80px] gap-2 items-center py-0.5 hover:bg-muted/30 px-1 rounded"
            title={tooltipFor(s)}
          >
            <span className={cn("uppercase tracking-wider truncate", KIND_TEXT_COLORS[s.kind])}>
              {s.kind}
            </span>
            <div className="relative h-4 bg-muted/20 rounded">
              <div
                className={cn(
                  "absolute h-full rounded border-l-2 transition-colors",
                  KIND_COLORS[s.kind],
                  inFlight && "animate-pulse opacity-60",
                  s.error && "ring-1 ring-rose-400",
                )}
                style={{
                  left: `${leftPct}%`,
                  width: `${widthPct}%`,
                }}
              />
              <span
                className={cn(
                  "absolute inset-0 flex items-center px-2 truncate text-[9px] text-foreground/90 pointer-events-none",
                  // Right-justify the label outside the bar if the bar is
                  // narrow (< 25% of canvas), so labels stay readable
                  // even for fast calls.
                  widthPct < 25 ? "" : "",
                )}
                style={{
                  left: widthPct < 25 ? `calc(${leftPct}% + ${widthPct}% + 4px)` : `calc(${leftPct}% + 4px)`,
                }}
              >
                {s.name}
              </span>
            </div>
            <span className="text-right text-muted-foreground tabular-nums">
              {inFlight ? `${formatDuration(s.durationMs)}+` : formatDuration(s.durationMs)}
            </span>
          </div>
        );
      })}
    </div>
  );
}


function TimeAxis({ totalDurationMs }: { totalDurationMs: number }) {
  // Five tick marks: 0, 25%, 50%, 75%, 100% of totalDuration
  const ticks = [0, 0.25, 0.5, 0.75, 1.0].map((f) => ({
    pct: f * 100,
    ms: f * totalDurationMs,
  }));
  return (
    <div className="grid grid-cols-[120px_1fr_80px] gap-2 items-center pb-1 border-b border-border/50 mb-1 text-[9px] text-muted-foreground">
      <span></span>
      <div className="relative h-3">
        {ticks.map((t, i) => (
          <span
            key={i}
            className="absolute -translate-x-1/2 tabular-nums"
            style={{ left: `${t.pct}%` }}
          >
            {formatDuration(t.ms)}
          </span>
        ))}
      </div>
      <span className="text-right">duration</span>
    </div>
  );
}


function formatDuration(ms: number): string {
  if (ms < 1) return "<1ms";
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}


function tooltipFor(s: CallSpan): string {
  const lines: string[] = [`${s.kind} · ${s.name}`];
  lines.push(`start: +${formatDuration(s.startMs)}`);
  lines.push(`duration: ${formatDuration(s.durationMs)}${s.endMs === null ? " (in-flight)" : ""}`);
  if (s.runId) lines.push(`run_id: ${s.runId.slice(0, 18)}…`);
  if (s.error) lines.push(`error: ${s.error}`);
  if (s.extra && typeof s.extra === "object") {
    for (const [k, v] of Object.entries(s.extra)) {
      if (k === "error") continue;
      const vs = String(v);
      lines.push(`${k}: ${vs.slice(0, 100)}${vs.length > 100 ? "…" : ""}`);
    }
  }
  return lines.join("\n");
}
