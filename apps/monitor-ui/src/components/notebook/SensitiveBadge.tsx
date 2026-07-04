/**
 * SensitiveBadge — the reader-side trust signal for a credential-safe note
 * (Grimoire, tasks/grimoire/SENSITIVE-AND-FLAGS.md Feature A). `raw_text`
 * (shown in the reader) is always the real, human-readable text — this
 * banner communicates that the LLM-facing surfaces (structuring, chat,
 * embedding, training export) only ever see a redacted twin.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import type { NoteRedaction } from "@/components/notebook/notebookTypes";

export function SensitiveBanner({ redactions }: { redactions: NoteRedaction[] | null }) {
  const [showHidden, setShowHidden] = useState(false);
  const count = redactions?.length ?? 0;

  return (
    <div className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-2.5">
      <p className="text-[11px] font-medium text-amber-400">
        🔒 Sensitive — the assistant sees a redacted copy
      </p>
      <p className="mt-0.5 text-[10px] text-muted-foreground/80">
        This reader shows the real text. Structuring, chat, embedding, and training export only
        ever see a version with secrets masked out.
      </p>
      {count > 0 ? (
        <div className="mt-1.5">
          <button
            type="button"
            onClick={() => setShowHidden((v) => !v)}
            className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
          >
            {showHidden ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            what's hidden ({count})
          </button>
          {showHidden ? (
            <ul className="mt-1 space-y-0.5 pl-4">
              {redactions?.map((r, i) => (
                <li key={`${r.placeholder}-${i}`} className="font-mono text-[10px] text-muted-foreground/80">
                  {r.placeholder} <span className="text-muted-foreground/50">({r.kind})</span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
