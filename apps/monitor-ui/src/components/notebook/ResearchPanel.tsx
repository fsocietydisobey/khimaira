/**
 * ResearchPanel — shared display pieces for the grimoire's research-scientist
 * output (Phase 3), reused by the per-guide chat panel (`GuideChatPanel`):
 * citation chips, the cost/grounding footer, and the REVISE line diff.
 *
 * The two-button ANSWER/REVISE toolbar this file used to hold (`GuideResearchAsk`,
 * `useGuideRevise`, `ReviseResultPanel`) is RETIRED — Joseph locked a per-guide
 * conversational chat design instead (tasks/grimoire/CHAT-MODEL.md, decision
 * e2fba504). Those called `POST /notes/research` / `POST /notes/{id}/research-
 * revise` synchronously; void-null made research async (job+poll) to survive
 * long agentic calls, breaking that contract, and the chat panel supersedes it
 * entirely rather than adapting it. Deleted rather than left dead per the
 * project's dead-code rule — see `GuideChatPanel.tsx` for the replacement.
 */

import { AlertTriangle, Globe } from "lucide-react";
import { diffLines } from "diff";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** The fields every citation/cost/grounding display needs from an agentic
 *  research result — a chat message (role/content/edit/ts + these) can pass
 *  itself directly without reshaping.
 *
 *  `code_citations`/`web_citations` are plain strings, NOT objects — byte-
 *  verified 2026-07-04 against a real live call: code citations are
 *  `"file_path:line"` (e.g. `"backend/core/services/kg/traversal.py:32"`);
 *  web citations are bare URLs. An earlier draft assumed a `CodeSource`-
 *  shaped object (mirroring `/notes/ask`'s `code_sources`) — wrong, caught
 *  via the rendered UI showing "undefined/undefined:undefined-undefined".
 *  Carry this shape forward into the chat contract's message fields. */
export interface ResearchMeta {
  code_citations: string[];
  web_citations: string[];
  total_cost_usd: number | null;
  web_grounded: boolean;
  web_grounding_unverified: boolean;
}

export function ResearchFooter({ result }: { result: ResearchMeta }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-border/50 pt-2">
      {result.total_cost_usd != null ? (
        <Badge variant="outline" className="text-[10px] text-muted-foreground">
          ${result.total_cost_usd.toFixed(2)}
        </Badge>
      ) : null}
      {result.web_grounding_unverified ? (
        // The trust signal — never suppress this, even though it reads as a
        // caveat. An answer that LOOKS web-grounded but isn't is worse than
        // one that visibly flags the gap.
        <Badge
          variant="outline"
          className="gap-1 border-amber-500/50 text-[10px] text-amber-400"
        >
          <AlertTriangle className="h-3 w-3" />
          grounding unverified
        </Badge>
      ) : result.web_grounded ? (
        <Badge variant="outline" className="border-emerald-500/40 text-[10px] text-emerald-400">
          web-grounded
        </Badge>
      ) : null}
    </div>
  );
}

/** Code citations are plain "file_path:line" strings (verified live against
 *  a real backend call, not a richer CodeSource shape) — render as-is, no
 *  field parsing. */
function CodeCitationChip({ citation }: { citation: string }) {
  return (
    <Badge
      variant="outline"
      className="max-w-full truncate font-mono text-[10px] text-sky-300/90"
      title={citation}
    >
      {citation}
    </Badge>
  );
}

/** Web citations are bare URL strings (same live-verified pattern as code
 *  citations). */
function WebCitationChip({ url }: { url: string }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title={url}
      className="inline-flex max-w-[240px] items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[10px] text-sky-400 hover:bg-accent/50 hover:text-sky-300"
    >
      <Globe className="h-2.5 w-2.5 shrink-0" />
      <span className="truncate">{url}</span>
    </a>
  );
}

export function CitationsRow({
  result,
}: {
  result: Pick<ResearchMeta, "code_citations" | "web_citations">;
}) {
  if (result.code_citations.length === 0 && result.web_citations.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      {result.code_citations.map((c, i) => (
        <CodeCitationChip key={`code-${c}-${i}`} citation={c} />
      ))}
      {result.web_citations.map((url, i) => (
        <WebCitationChip key={`web-${url}-${i}`} url={url} />
      ))}
    </div>
  );
}

/** Line-level diff between a guide's raw_text before/after an edit. Uses
 *  `diff` (jsdiff) — small, standard, no existing dependency covered this
 *  need. Reused by the chat panel's edit-message bubbles (an auto-applied
 *  edit shows this diff inline, per the locked chat design). */
export function DiffView({ current, proposed }: { current: string; proposed: string }) {
  const parts = diffLines(current, proposed);
  return (
    <div className="min-w-0 overflow-x-auto rounded border border-border bg-background/40 p-3 font-mono text-[11px] leading-relaxed">
      {parts.map((part, i) => (
        <div
          key={i}
          className={cn(
            "whitespace-pre-wrap [overflow-wrap:anywhere]",
            part.added && "bg-emerald-500/15 text-emerald-200",
            part.removed && "bg-rose-500/15 text-rose-300/90 line-through decoration-rose-500/40",
          )}
        >
          {part.value}
        </div>
      ))}
    </div>
  );
}
