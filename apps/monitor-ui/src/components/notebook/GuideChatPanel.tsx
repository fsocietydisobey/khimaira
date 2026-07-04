/**
 * GuideChatPanel — the per-guide conversational chat (Grimoire Phase 3
 * redesign, tasks/grimoire/CHAT-MODEL.md, Joseph's decision e2fba504).
 * Replaces the retired two-button ANSWER/REVISE toolbar: one persistent
 * chat per guide, scoped like a Claude Code session — researches + answers
 * by default, auto-applies edits on instruction (diff shown inline, undo
 * via version history), multi-turn, with clear + compact controls.
 *
 * Wire: Send → POST /notes/{id}/chat {message} → {job_id} → poll
 * GET /notes/research/{job_id} (kind:"chat") until done/error → refetch
 * the persisted history (source of truth) rather than hand-constructing
 * the new turn from the poll response, so rendering never depends on
 * guessing whether the poll's transient shape matches the stored shape.
 * An optimistic local echo covers the ~1-2 min gap before that refetch.
 *
 * Split into a hook (`useGuideChat`) + two presentational halves
 * (`GuideChatHeaderControls`, `GuideChatBody`) rather than one component
 * with its own header, so the caller can put Clear/Compact into the
 * enclosing SidePanelShell's single header bar (`extraHeader`) instead of
 * stacking a second "chat" header underneath it — the same split already
 * used for the retired REVISE panel (useGuideRevise/ReviseResultPanel).
 */

import { skipToken } from "@reduxjs/toolkit/query";
import { useEffect, useRef, useState } from "react";
import { AlertCircle, Eraser, Loader2, Sparkles } from "lucide-react";

import {
  useClearChatMutation,
  useCompactChatMutation,
  useGetChatHistoryQuery,
  usePollChatJobQuery,
  useSendChatMessageMutation,
} from "@/api";
import { MarkdownView } from "@/components/notebook/MarkdownView";
import type { ChatMessage } from "@/components/notebook/notebookTypes";
import { CitationsRow, ResearchFooter } from "@/components/notebook/ResearchPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const POLL_INTERVAL_MS = 3000;

export function useGuideChat(noteId: string) {
  const { data: historyData, refetch: refetchHistory } = useGetChatHistoryQuery(noteId);
  const [sendChatMessage] = useSendChatMessageMutation();
  const [clearChat, { isLoading: clearing }] = useClearChatMutation();
  const [compactChat, { isLoading: compacting }] = useCompactChatMutation();

  const [input, setInput] = useState("");
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [pendingUserMessage, setPendingUserMessage] = useState<string | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);

  const { data: jobData } = usePollChatJobQuery(activeJobId ?? skipToken, {
    pollingInterval: activeJobId ? POLL_INTERVAL_MS : 0,
  });

  useEffect(() => {
    if (!jobData || jobData.status === "pending") return;
    setActiveJobId(null);
    setPendingUserMessage(null);
    if (jobData.status === "error") {
      setJobError(jobData.error || "chat turn failed — try again.");
      return;
    }
    setJobError(null);
    // History is the source of truth (the backend persisted both the user
    // turn and this response) — refetch rather than hand-construct the
    // message from the poll's transient shape.
    refetchHistory();
  }, [jobData, refetchHistory]);

  const busy = activeJobId !== null;
  const history = historyData?.history ?? [];

  const handleSend = async () => {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setPendingUserMessage(message);
    setJobError(null);
    try {
      const { job_id } = await sendChatMessage({ id: noteId, message }).unwrap();
      setActiveJobId(job_id);
    } catch {
      setPendingUserMessage(null);
      setJobError("couldn't start the chat turn — try again.");
    }
  };

  const handleClear = async () => {
    if (busy || clearing) return;
    await clearChat(noteId).unwrap();
  };

  const handleCompact = async () => {
    if (busy || compacting) return;
    await compactChat(noteId).unwrap();
  };

  return {
    history,
    busy,
    clearing,
    compacting,
    pendingUserMessage,
    jobError,
    input,
    setInput,
    handleSend,
    handleClear,
    handleCompact,
  };
}

type GuideChatState = ReturnType<typeof useGuideChat>;

/** Clear/Compact — rendered into the enclosing SidePanelShell's `extraHeader`
 *  slot, NOT a header of its own (that was Bug 2: two stacked "chat" bars). */
export function GuideChatHeaderControls({ state }: { state: GuideChatState }) {
  return (
    <div className="flex items-center gap-0.5">
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="h-6 gap-1 px-1.5 text-[9px]"
        onClick={state.handleClear}
        disabled={state.busy || state.clearing || state.history.length === 0}
      >
        <Eraser className="h-3 w-3" />
        clear
      </Button>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="h-6 px-1.5 text-[9px]"
        onClick={state.handleCompact}
        disabled={state.busy || state.compacting || state.history.length <= 4}
        title={state.history.length <= 4 ? "nothing to compact yet" : undefined}
      >
        compact
      </Button>
    </div>
  );
}

/** Renders a chat edit's pre-formatted diff string with unified-diff-style
 *  +/- line coloring. UNVERIFIED assumption (see ChatEdit in notebookTypes.ts)
 *  — degrades to plain monospace text if the backend's format differs, since
 *  an unrecognized prefix just falls through to the neutral style. */
function ChatEditDiff({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <div className="min-w-0 max-w-full overflow-x-auto rounded border border-border bg-background/40 p-2 font-mono text-[10px] leading-relaxed">
      {lines.map((line, i) => (
        <div
          key={i}
          className={cn(
            "whitespace-pre-wrap [overflow-wrap:anywhere]",
            line.startsWith("+") && !line.startsWith("+++") && "bg-emerald-500/15 text-emerald-200",
            line.startsWith("-") && !line.startsWith("---") && "bg-rose-500/15 text-rose-300/90",
          )}
        >
          {line || " "}
        </div>
      ))}
    </div>
  );
}

function ChatBubble({ message }: { message: ChatMessage }) {
  if (message.role === "system") {
    // compact's summary message — a distinct divider, not a normal bubble.
    return (
      <div className="flex min-w-0 items-center gap-2 py-1 text-[10px] text-muted-foreground/60">
        <div className="h-px flex-1 bg-border/50" />
        <span className="shrink-0">— summarized —</span>
        <div className="h-px flex-1 bg-border/50" />
      </div>
    );
  }

  const isUser = message.role === "user";
  return (
    <div className={cn("flex min-w-0", isUser ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "min-w-0 max-w-full overflow-hidden break-words rounded-lg px-3 py-2 text-xs [overflow-wrap:anywhere] sm:max-w-[88%]",
          isUser ? "bg-accent text-accent-foreground" : "border border-border bg-card/60",
        )}
      >
        <MarkdownView content={message.content} />
        {message.edit ? (
          <div className="mt-2 min-w-0 border-t border-border/50 pt-2">
            <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
              <Badge variant="outline" className="border-emerald-500/40 text-[9px] text-emerald-400">
                ✓ applied
                {message.edit.applied_at
                  ? ` · ${new Date(message.edit.applied_at).toLocaleTimeString()}`
                  : ""}
              </Badge>
              {message.edit.section_anchor ? (
                <span className="min-w-0 truncate text-[9px] text-muted-foreground/70">
                  section: {message.edit.section_anchor}
                </span>
              ) : null}
            </div>
            <ChatEditDiff diff={message.edit.diff} />
          </div>
        ) : null}
        {!isUser && message.grounding ? <CitationsRow result={message.grounding} /> : null}
        {!isUser && message.grounding ? (
          <ResearchFooter result={{ ...message.grounding, total_cost_usd: message.cost ?? null }} />
        ) : null}
      </div>
    </div>
  );
}

/** Message list + input — the SidePanelShell wrapping this owns the "chat"
 *  header bar (label + collapse chevron + GuideChatHeaderControls); this
 *  component renders none of that itself (Bug 2 fix). */
export function GuideChatBody({ state }: { state: GuideChatState }) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [state.history, state.pendingUserMessage, state.busy]);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div ref={scrollRef} className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto p-2">
        {state.history.length === 0 && !state.pendingUserMessage ? (
          <div className="flex h-full flex-col items-center justify-center gap-1.5 p-4 text-center">
            <Sparkles className="h-4 w-4 text-muted-foreground/50" />
            <p className="text-[10px] text-muted-foreground/60">
              ask a question, or tell it to change something — e.g. "add a section on X".
            </p>
          </div>
        ) : (
          <div className="min-w-0 space-y-2">
            {state.history.map((m, i) => (
              <ChatBubble key={`${m.role}-${m.ts ?? i}`} message={m} />
            ))}
            {state.pendingUserMessage ? (
              <ChatBubble message={{ role: "user", content: state.pendingUserMessage }} />
            ) : null}
            {state.busy ? (
              <div className="flex items-center gap-1.5 px-1 py-1 text-[10px] text-muted-foreground">
                <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
                <span className="min-w-0 truncate">researching the codebase / searching the web…</span>
              </div>
            ) : null}
            {state.jobError ? (
              <div className="flex min-w-0 items-center gap-1.5 rounded border border-destructive/40 bg-destructive/5 px-2 py-1.5 text-[10px] text-destructive">
                <AlertCircle className="h-3 w-3 shrink-0" />
                <span className="min-w-0 break-words [overflow-wrap:anywhere]">{state.jobError}</span>
              </div>
            ) : null}
          </div>
        )}
      </div>

      <div className="shrink-0 border-t border-border/50 p-2">
        <div className="flex items-center gap-1.5">
          <input
            value={state.input}
            onChange={(e) => state.setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                state.handleSend();
              }
            }}
            placeholder="ask, or instruct — e.g. 'add a benchmark'"
            disabled={state.busy}
            className="min-w-0 flex-1 rounded border border-border bg-background/60 px-2 py-1.5 text-xs outline-none focus:border-ring disabled:opacity-50"
          />
          <Button
            type="button"
            size="sm"
            className="h-8 shrink-0 px-2 text-[10px]"
            onClick={state.handleSend}
            disabled={state.busy || !state.input.trim()}
          >
            {state.busy ? <Loader2 className="h-3 w-3 animate-spin" /> : "send"}
          </Button>
        </div>
      </div>
    </div>
  );
}
