/**
 * MermaidBlock — renders a ```mermaid fenced code block as an actual SVG
 * diagram via mermaid.js. Falls back to the raw source on any parse/render
 * error rather than showing a blank box or crashing the note view.
 */

import { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";

mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });

let mermaidIdCounter = 0;

/** code → already-rendered output. Belt-and-suspenders against ANY future
 *  re-render source remounting this component with identical `code` (the
 *  2026-07-04 flicker regression was an unconditional 3s reader poll causing
 *  exactly this) — a cache hit reuses the SVG instantly instead of paying
 *  mermaid.render again. Module-level (not per-instance) since the same
 *  diagram source can appear in more than one note/guide. */
const mermaidCache = new Map<string, { svg: string; bindFunctions?: (el: Element) => void }>();

export function MermaidBlock({ code }: { code: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const idRef = useRef(`mermaid-diagram-${mermaidIdCounter++}`);

  useEffect(() => {
    let cancelled = false;
    setError(null);

    const cached = mermaidCache.get(code);
    if (cached) {
      if (containerRef.current) {
        containerRef.current.innerHTML = cached.svg;
        cached.bindFunctions?.(containerRef.current);
      }
      return;
    }

    mermaid
      .render(idRef.current, code)
      .then(({ svg, bindFunctions }) => {
        if (cancelled) return;
        mermaidCache.set(code, { svg, bindFunctions });
        if (!containerRef.current) return;
        containerRef.current.innerHTML = svg;
        bindFunctions?.(containerRef.current);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [code]);

  if (error) {
    return (
      <div className="my-2 min-w-0">
        <p className="mb-1 text-[10px] text-destructive/80">
          mermaid diagram failed to render — showing raw source
        </p>
        <pre className="overflow-x-auto rounded bg-background/60 p-2 text-[11px]">
          <code>{code}</code>
        </pre>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="my-2 min-w-0 overflow-x-auto rounded border border-border/50 bg-background/40 p-3 [&_svg]:mx-auto"
    />
  );
}
