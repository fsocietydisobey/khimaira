/**
 * TestStatusSelector — a human testing-workflow status selector for notes
 * (Joseph, 2026-07-07). `test_status` is independent of `status` (the
 * automated structuring pipeline's lifecycle) and of the read-only
 * `lifecycle` projection — same badge-column region as PrioritySelector.
 *
 * Notes only, by design: study guides keep their own housed/organized
 * lifecycle for a different concept (library placement) — this selector is
 * never rendered in LibraryView/GuideReader.
 *
 * A native `<select>`, same precedent as PrioritySelector — the emoji
 * prefix on each option already shows the state in the closed control
 * without a separate badge element.
 */

import type { NoteTestStatus } from "@/components/notebook/notebookTypes";
import { cn } from "@/lib/utils";

const TEST_STATUS_ORDER: NoteTestStatus[] = [
  "untested",
  "needs_testing",
  "in_review",
  "tested",
];

const TEST_STATUS_DOT: Record<NoteTestStatus, string> = {
  untested: "⚪",
  needs_testing: "🟡",
  in_review: "🔵",
  tested: "🟢",
};

const TEST_STATUS_LABEL: Record<NoteTestStatus, string> = {
  untested: "untested",
  needs_testing: "needs testing",
  in_review: "in review",
  tested: "tested",
};

export function testStatusDot(status: NoteTestStatus): string {
  return TEST_STATUS_DOT[status];
}

export function TestStatusSelector({
  testStatus,
  onChange,
  className,
}: {
  testStatus: NoteTestStatus;
  onChange: (s: NoteTestStatus) => void;
  className?: string;
}) {
  return (
    <select
      value={testStatus}
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => onChange(e.target.value as NoteTestStatus)}
      title="Testing status"
      className={cn(
        "h-5 shrink-0 rounded border border-input bg-background px-1 text-[9px] text-muted-foreground outline-none hover:text-foreground focus:ring-1 focus:ring-ring",
        className,
      )}
    >
      {TEST_STATUS_ORDER.map((s) => (
        <option key={s} value={s}>
          {TEST_STATUS_DOT[s]} {TEST_STATUS_LABEL[s]}
        </option>
      ))}
    </select>
  );
}
