/**
 * CopyJsonButton — copy an arbitrary object to the clipboard as pretty JSON.
 *
 * Shared by the node + edge inspectors so an agent (or human) can lift the
 * raw detail payload out of the panel for a bug report / further analysis.
 * Code-agnostic: it serializes whatever object it's handed, no schema knowledge.
 */

import { useState } from "react";

import { Button } from "@/components/ui/button";

export function CopyJsonButton({
  value,
  label = "copy JSON",
}: {
  value: unknown;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    try {
      navigator.clipboard.writeText(JSON.stringify(value, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      // Clipboard can reject (insecure context / permissions). Fail quietly —
      // a copy button that throws is worse than one that silently no-ops.
      setCopied(false);
    }
  };

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={handleCopy}
      className="h-6 px-2 text-[10px] text-muted-foreground hover:text-foreground"
      title="copy the raw detail JSON to the clipboard"
    >
      {copied ? "✓ copied" : label}
    </Button>
  );
}
