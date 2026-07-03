/**
 * MermaidBlock — renders a ```mermaid fenced code block as an actual SVG
 * diagram via mermaid.js. Falls back to the raw source on any parse/render
 * error rather than showing a blank box or crashing the note view.
 */

import { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";

mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });

let mermaidIdCounter = 0;

export function MermaidBlock({ code }: { code: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const idRef = useRef(`mermaid-diagram-${mermaidIdCounter++}`);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    mermaid
      .render(idRef.current, code)
      .then(({ svg, bindFunctions }) => {
        if (cancelled || !containerRef.current) return;
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
