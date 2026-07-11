/**
 * CopyRawMarkdownButton — one-click copy of a note/guide's raw markdown
 * (`raw_text`) to the clipboard. Icon-only, mirrors IdChip's copy/check
 * icon pattern (same lucide icons, same 1s "copied" flash) so it reads as
 * a natural sibling control wherever it sits next to an IdChip.
 *
 * Unlike IdChip, a FAILED copy shows a visible red X for 1.5s rather than
 * silently doing nothing — a silent failure here is indistinguishable
 * from success (both just "the icon doesn't change"), and the whole point
 * of a copy button is that the user stops looking and goes to paste
 * elsewhere. `navigator.clipboard.writeText` can reject for real reasons
 * (document not focused, permission revoked, non-secure context) and the
 * user needs to know when that happens, not silently get stale clipboard
 * content on paste.
 *
 * Also falls back to the legacy `document.execCommand("copy")` path (via
 * a temporary off-screen textarea) when the async Clipboard API throws —
 * that legacy path has looser focus requirements and covers browsers/
 * embeds where `navigator.clipboard` is unavailable or blocked.
 *
 * Deliberately takes `text` directly rather than a note id + its own query:
 * the note reader's "original" panel already holds the single-note-fetch
 * result (the one that returns REAL raw_text even for sensitive notes —
 * unlike the list/search projection, which redacts sensitive raw_text to a
 * placeholder), so the caller passes that same value through rather than
 * this component re-deriving it from a possibly-redacted source.
 */

import { useState } from "react";
import { Check, Copy, X } from "lucide-react";

import { cn } from "@/lib/utils";

function legacyCopy(text: string): boolean {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  // Off-screen, not display:none (some browsers refuse to select
  // display:none content for execCommand("copy")).
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(textarea);
  return ok;
}

type CopyState = "idle" | "copied" | "failed";

export function CopyRawMarkdownButton({
  text,
  className,
}: {
  text: string | null | undefined;
  className?: string;
}) {
  const [state, setState] = useState<CopyState>("idle");

  if (!text) return null;

  const flash = (next: CopyState) => {
    setState(next);
    setTimeout(() => setState("idle"), next === "failed" ? 1500 : 1000);
  };

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      flash("copied");
    } catch {
      flash(legacyCopy(text) ? "copied" : "failed");
    }
  };

  const titleByState: Record<CopyState, string> = {
    idle: "Copy raw markdown",
    copied: "Copied!",
    failed:
      "Copy failed — your browser blocked clipboard access. Select the text and copy manually.",
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      title={titleByState[state]}
      className={cn(
        "inline-flex shrink-0 items-center rounded p-0.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
        className,
      )}
    >
      {state === "copied" ? (
        <Check className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
      ) : state === "failed" ? (
        <X className="h-3.5 w-3.5 shrink-0 text-red-400" />
      ) : (
        <Copy className="h-3.5 w-3.5 shrink-0" />
      )}
    </button>
  );
}
