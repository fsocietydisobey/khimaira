/**
 * RunsDrawer — slide-out panel showing the full runs list (including
 * idle). Opened from the LiveRunsCard "all runs" button. Closes on
 * outside click or Escape.
 */

import { useEffect } from "react";

import { RunsSidebar } from "@/components/project/RunsSidebar";
import { Button } from "@/components/ui/button";

interface RunsDrawerProps {
  open: boolean;
  projectName: string;
  selectedThreadId: string | null;
  onSelectThread: (threadId: string | null) => void;
  onSelectRun?: (threadIds: string[]) => void;
  onClose: () => void;
}

export function RunsDrawer({
  open,
  projectName,
  selectedThreadId,
  onSelectThread,
  onSelectRun,
  onClose,
}: RunsDrawerProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        className="absolute inset-0 z-20 bg-background/60 backdrop-blur-sm"
        onClick={onClose}
      />
      {/* Drawer */}
      <div className="absolute top-0 left-0 z-30 h-full w-80 border-r border-border bg-card shadow-2xl flex flex-col">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <span className="text-xs font-semibold">All Runs</span>
          <Button variant="ghost" size="sm" onClick={onClose}>×</Button>
        </div>
        <div className="flex-1 min-h-0">
          <RunsSidebar
            projectName={projectName}
            selectedThreadId={selectedThreadId}
            onSelectThread={(id) => {
              onSelectThread(id);
              onClose();
            }}
            onSelectRun={onSelectRun}
          />
        </div>
      </div>
    </>
  );
}
