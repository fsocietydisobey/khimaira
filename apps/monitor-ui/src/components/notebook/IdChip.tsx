/**
 * IdChip — a copyable id handle shown on notes and study guides (cards +
 * both readers), so Joseph can point his roster master at a specific
 * record via `notebook_get(note_id=...)` / `GET /notes/{id}` without
 * hunting for the id in a URL or payload. Display + clipboard only — the
 * id is already the record's existing `id` field, no new scheme.
 *
 * Deliberately its own small file (not folded into Notebook.tsx or
 * LibraryView.tsx): it's about to sit alongside the coming priority-dot and
 * sensitive-🔒 badges in the same card meta row across both note and guide
 * surfaces, so it needs to be importable from both without a cross-import
 * between those two files.
 */

import { useState } from "react";
import { Check, Copy } from "lucide-react";

import { cn } from "@/lib/utils";

export function IdChip({ id, className }: { id: string; className?: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async (e: React.MouseEvent) => {
    // Cards bind onOpen to a click on an ancestor — don't also navigate.
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      setTimeout(() => setCopied(false), 1000);
    } catch {
      // Clipboard API unavailable/denied (e.g. insecure context) — silent,
      // no crash. The id is still visible to copy by hand.
    }
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      title="Copy id"
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded border border-border px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
        className,
      )}
    >
      {id}
      {copied ? (
        <Check className="h-2.5 w-2.5 shrink-0 text-emerald-400" />
      ) : (
        <Copy className="h-2.5 w-2.5 shrink-0" />
      )}
    </button>
  );
}
