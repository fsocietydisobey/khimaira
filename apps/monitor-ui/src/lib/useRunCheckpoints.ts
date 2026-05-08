/**
 * useRunCheckpoints — fetch and merge a chronological checkpoint
 * timeline for a "run" = one primary thread + zero-or-more sibling
 * threads (same logical execution that may have spanned multiple
 * subgraphs).
 *
 * Output: a single RunCheckpoint[] sorted oldest → newest, with each
 * entry tagged by its source `thread_id` so callers can map back to
 * the originating graph (needed for cross-graph step numbering and
 * ghost rendering).
 *
 * Live tailing: subscribes to `/api/threads/{name}/{thread_id}/stream`
 * via EventSource. The daemon polls the DB at 250ms and pushes new
 * checkpoints — fast nodes (`route_after_*`, sub-second classifiers)
 * that the old 2s polling would miss now show up. Siblings are fetched
 * once per key change because they belong to a fixed (already-finished)
 * subgraph; no need to stream them.
 *
 * The `pollingInterval` argument is kept for back-compat / fallback;
 * when SSE is enabled (default) it's ignored. Set
 * `options.useStreaming = false` to fall back to RTK Query polling.
 */

import { useEffect, useMemo, useState } from "react";

import type { CheckpointDetail } from "@/api";
import { useGetThreadDetailQuery } from "@/api";

export interface RunCheckpoint extends CheckpointDetail {
  thread_id: string;
}

interface Options {
  useStreaming?: boolean;
}

export function useRunCheckpoints(
  projectName: string,
  primaryThreadId: string | null,
  siblingThreadIds: string[],
  pollingInterval: number,
  options: Options = {},
): { checkpoints: RunCheckpoint[]; isLoading: boolean } {
  const useStreaming = options.useStreaming ?? true;

  // Initial / cold-start fetch. When streaming is on, polling is disabled
  // (interval 0) — SSE handles updates from here on.
  const { data, isLoading } = useGetThreadDetailQuery(
    { name: projectName, threadId: primaryThreadId ?? "", limit: 200 },
    {
      skip: !primaryThreadId,
      pollingInterval: useStreaming ? 0 : pollingInterval,
    },
  );

  // SSE-appended checkpoints. Keyed implicitly by checkpoint_id; we
  // dedup against the RTK Query baseline in the merge step below.
  const [live, setLive] = useState<RunCheckpoint[]>([]);
  useEffect(() => {
    if (!useStreaming || !primaryThreadId) {
      setLive([]);
      return;
    }
    setLive([]);  // reset whenever the thread we're watching changes
    const url =
      `/api/threads/${encodeURIComponent(projectName)}` +
      `/${encodeURIComponent(primaryThreadId)}/stream`;
    const es = new EventSource(url);

    const onCheckpoint = (event: MessageEvent) => {
      try {
        const payload = JSON.parse(event.data) as CheckpointDetail;
        setLive((prev) => [
          ...prev,
          { ...payload, thread_id: primaryThreadId },
        ]);
      } catch (e) {
        console.warn("useRunCheckpoints: failed to parse SSE event", e);
      }
    };
    const onIdleTimeout = () => {
      // Server stopped sending after 30min — close cleanly. The next
      // user interaction will re-mount the component and reconnect.
      es.close();
    };

    es.addEventListener("checkpoint", onCheckpoint as EventListener);
    es.addEventListener("idle_timeout", onIdleTimeout as EventListener);

    return () => {
      es.removeEventListener("checkpoint", onCheckpoint as EventListener);
      es.removeEventListener("idle_timeout", onIdleTimeout as EventListener);
      es.close();
    };
  }, [projectName, primaryThreadId, useStreaming]);

  const [siblings, setSiblings] = useState<RunCheckpoint[]>([]);
  const siblingsKey = siblingThreadIds.slice().sort().join(",");
  useEffect(() => {
    if (siblingThreadIds.length === 0) {
      setSiblings([]);
      return;
    }
    let cancelled = false;
    Promise.all(
      siblingThreadIds.map(async (tid) => {
        try {
          const resp = await fetch(
            `/api/threads/${encodeURIComponent(projectName)}/${encodeURIComponent(tid)}?limit=200`,
          );
          if (!resp.ok) return [] as RunCheckpoint[];
          const json = await resp.json();
          const list = (json.checkpoints ?? []) as CheckpointDetail[];
          return list.map((c) => ({ ...c, thread_id: tid }));
        } catch {
          return [] as RunCheckpoint[];
        }
      }),
    ).then((arrays) => {
      if (cancelled) return;
      setSiblings(arrays.flat());
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectName, siblingsKey]);

  const merged = useMemo<RunCheckpoint[]>(() => {
    const primary: RunCheckpoint[] =
      data && primaryThreadId
        ? [...data.checkpoints]
            .reverse()
            .map((c) => ({ ...c, thread_id: primaryThreadId }))
        : [];

    // Dedup live events that overlap with the RTK baseline (the initial
    // SSE event may repeat the latest fetched checkpoint).
    const seen = new Set(primary.map((c) => c.checkpoint_id));
    const liveNew = live.filter((c) => !seen.has(c.checkpoint_id));

    if (siblings.length === 0 && liveNew.length === 0) return primary;
    const all = [...primary, ...liveNew, ...siblings];
    all.sort((a, b) =>
      (a.created_at ?? "").localeCompare(b.created_at ?? ""),
    );
    return all;
  }, [data, primaryThreadId, siblings, live]);

  return { checkpoints: merged, isLoading };
}
