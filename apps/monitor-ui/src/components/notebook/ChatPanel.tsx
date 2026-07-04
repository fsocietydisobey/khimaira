/**
 * ChatPanel — the ONE shared chat sidebar across the grimoire (Grimoire
 * Phase 3 + CHAT-UNIFY, tasks/grimoire/CHAT-UNIFY.md). Scope follows what's
 * open (Joseph, decision — context-aware):
 *   - Guide open  → per-record chat scoped to that guide.
 *   - Note open   → per-record chat scoped to that note (same backend route,
 *                   the guide-only 404 was lifted).
 *   - Nothing open → notebook-wide ask across all notes, `@` to reference
 *                   one. Retires the old top ask-bar.
 *
 * Two hooks, ONE presentation:
 *   - `useRecordChat(noteId | null)` — persistent (GET/POST /notes/{id}/chat),
 *     agentic, may auto-apply an edit. `noteId===null` is inert (skipToken),
 *     so this can always be called unconditionally alongside useNotebookChat
 *     without violating the rules of hooks.
 *   - `useNotebookChat(notes)` — one-shot (POST /notes/chat), client-
 *     accumulated only (no server history, per CHAT-UNIFY's MVP decision),
 *     `@`-ref autocomplete reusing the retired AskBar's mention logic.
 * Both return the same `ChatPanelState` shape; `ChatBody`/`ChatHeaderControls`
 * render whichever is passed in, branching on `mode` only for the handful of
 * things that generuinely differ (mention input, compact button, sensitive
 * notice, empty-state copy).
 */

import { skipToken } from "@reduxjs/toolkit/query";
import { useEffect, useRef, useState } from "react";
import { useDispatch } from "react-redux";
import { AlertCircle, Eraser, Loader2, Sparkles, X } from "lucide-react";

import {
  monitorApi,
  useClearChatMutation,
  useCompactChatMutation,
  useGetChatHistoryQuery,
  usePollChatJobQuery,
  useSendChatMessageMutation,
  useSendNotebookChatMutation,
} from "@/api";
import { MarkdownView } from "@/components/notebook/MarkdownView";
import type { ChatMessage, Note } from "@/components/notebook/notebookTypes";
import { CitationsRow, ResearchFooter } from "@/components/notebook/ResearchPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const POLL_INTERVAL_MS = 3000;

/** Suggested prompts shown in the notebook-wide empty state — moved here
 *  from the retired top ask-bar so they aren't lost. */
export const SUGGESTED_QUESTIONS = [
  "What's changed recently in the code this notebook tracks?",
  "Summarize everything tagged as a bug or incident.",
  "What's still unresolved or open?",
];

export interface ChatPanelState {
  messages: ChatMessage[];
  busy: boolean;
  pendingUserMessage: string | null;
  jobError: string | null;
  input: string;
  setInput: (v: string) => void;
  handleSend: (text?: string) => void;
  handleClear: () => void;
  clearing: boolean;
  /** Undefined hides the Compact button entirely — notebook-wide has no
   *  server-side history to compact (one-shot, client-accumulated only). */
  handleCompact?: () => void;
  compacting?: boolean;
  /** Notebook-wide only — @-ref autocomplete state, read/written by ChatBody. */
  mentions?: Mention[];
  setMentions?: (m: Mention[] | ((prev: Mention[]) => Mention[])) => void;
}

type Mention = { id: string; title: string };

/** Per-record (guide OR note) persistent chat. `noteId===null` is inert —
 *  every query skips, so this can be called unconditionally even when
 *  nothing is open (satisfies the rules of hooks alongside useNotebookChat). */
export function useRecordChat(noteId: string | null): ChatPanelState {
  const dispatch = useDispatch();
  const { data: historyData, refetch: refetchHistory } = useGetChatHistoryQuery(
    noteId ?? skipToken,
  );
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
    // An applied edit changed raw_text server-side and kicked off an async
    // reprocess — invalidate the note (both the single-record cache entry
    // and the list, which the guide grid + Notebook's note list read from)
    // so every reader picks up the new raw_text and starts its own
    // reprocess-visibility poll, exactly as it would after an edit made
    // through the record's own edit UI.
    if (noteId && jobData.message?.edit) {
      dispatch(
        monitorApi.util.invalidateTags([
          { type: "Notes", id: noteId },
          { type: "Notes", id: "LIST" },
        ]),
      );
    }
  }, [jobData, refetchHistory, dispatch, noteId]);

  const busy = activeJobId !== null;

  const handleSend = async (text?: string) => {
    if (!noteId) return;
    const message = (text ?? input).trim();
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
    if (!noteId || busy || clearing) return;
    await clearChat(noteId).unwrap();
  };

  const handleCompact = async () => {
    if (!noteId || busy || compacting) return;
    await compactChat(noteId).unwrap();
  };

  return {
    messages: historyData?.history ?? [],
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

/** Notebook-wide (nothing open) — one-shot per CHAT-UNIFY's MVP decision, no
 *  server-side history, so messages are client-accumulated for the session
 *  only (reopening loses them — flagged in the empty state, not hidden).
 *  Takes no args — the mention-search note pool is a `ChatBody` prop
 *  (`notes`), not hook state, since the hook only needs the selected ids. */
export function useNotebookChat(): ChatPanelState {
  const [sendNotebookChat] = useSendNotebookChatMutation();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [mentions, setMentions] = useState<Mention[]>([]);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);

  const { data: jobData } = usePollChatJobQuery(activeJobId ?? skipToken, {
    pollingInterval: activeJobId ? POLL_INTERVAL_MS : 0,
  });

  useEffect(() => {
    if (!jobData || jobData.status === "pending") return;
    setActiveJobId(null);
    if (jobData.status === "error") {
      setJobError(jobData.error || "ask failed — try again.");
      return;
    }
    setJobError(null);
    setMessages((prev) => [
      ...prev,
      {
        role: "assistant",
        content: jobData.answer ?? "",
        sources: jobData.sources ?? [],
        healed: jobData.healed ?? [],
        codeSources: (jobData.code_sources ?? []).map(
          (c) => `${c.file_path}:${c.start_line}-${c.end_line}`,
        ),
        codeUnavailable: jobData.code_unavailable ?? [],
      },
    ]);
  }, [jobData]);

  const busy = activeJobId !== null;

  const handleSend = async (text?: string) => {
    const message = (text ?? input).trim();
    if (!message || busy) return;
    setInput("");
    setJobError(null);
    const refs = mentions.map((m) => m.id);
    setMentions([]);
    setMessages((prev) => [...prev, { role: "user", content: message }]);
    try {
      const { job_id } = await sendNotebookChat({ message, refs }).unwrap();
      setActiveJobId(job_id);
    } catch {
      setJobError("couldn't start the ask — try again.");
    }
  };

  const handleClear = () => {
    if (busy) return;
    setMessages([]);
  };

  return {
    messages,
    busy,
    clearing: false,
    // pendingUserMessage stays null — the user turn is already pushed into
    // `messages` directly above (there's no server history to reconcile
    // against, unlike per-record chat).
    pendingUserMessage: null,
    jobError,
    input,
    setInput,
    handleSend,
    handleClear,
    // no handleCompact — hides the button (nothing server-side to compact).
    mentions,
    setMentions,
  };
}

/** Clear/Compact — rendered into the enclosing SidePanelShell's `extraHeader`
 *  slot, NOT a header of its own (two stacked "chat" headers was Bug 2 on
 *  the original guide-only build). */
export function ChatHeaderControls({ state }: { state: ChatPanelState }) {
  return (
    <div className="flex items-center gap-0.5">
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="h-6 gap-1 px-1.5 text-[9px]"
        onClick={state.handleClear}
        disabled={state.busy || state.clearing || state.messages.length === 0}
      >
        <Eraser className="h-3 w-3" />
        clear
      </Button>
      {state.handleCompact ? (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-1.5 text-[9px]"
          onClick={state.handleCompact}
          disabled={state.busy || state.compacting || state.messages.length <= 4}
          title={state.messages.length <= 4 ? "nothing to compact yet" : undefined}
        >
          compact
        </Button>
      ) : null}
    </div>
  );
}

/** Renders a chat edit's pre-formatted diff string with unified-diff-style
 *  +/- line coloring. Format assumption verified against a live backend call
 *  (2026-07-04) — see the `ChatEdit` doc comment in notebookTypes.ts. */
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

/** Notebook-wide citation chips — note ids cited + code chunks + a "healed"
 *  badge, mirroring the retired ask-bar's answer-view rendering. Distinct
 *  from CitationsRow (which renders per-record ChatGrounding) since the
 *  underlying data (note ids vs plain code_citations strings) differs. */
function NotebookWideCitations({ message }: { message: ChatMessage }) {
  const hasSources = (message.sources?.length ?? 0) > 0;
  const hasCode = (message.codeSources?.length ?? 0) > 0;
  const hasHealed = (message.healed?.length ?? 0) > 0;
  const hasUnavailable = (message.codeUnavailable?.length ?? 0) > 0;
  if (!hasSources && !hasCode && !hasHealed && !hasUnavailable) return null;

  return (
    <div className="mt-2 border-t border-border/50 pt-2">
      {hasSources || hasHealed ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {message.sources?.map((id) => (
            <Badge key={id} variant="outline" className="max-w-full truncate text-[9px]">
              {id}
            </Badge>
          ))}
          {hasHealed ? (
            <Badge variant="warning" className="text-[9px]">
              healed {message.healed?.length} note{message.healed?.length === 1 ? "" : "s"} vs
              current code
            </Badge>
          ) : null}
        </div>
      ) : null}
      {hasCode ? (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {message.codeSources?.map((c, i) => (
            <Badge
              key={`${c}-${i}`}
              variant="outline"
              className="max-w-full truncate font-mono text-[9px] text-sky-300/90"
              title={c}
            >
              {c}
            </Badge>
          ))}
        </div>
      ) : null}
      {hasUnavailable ? (
        <p className="mt-1 text-[9px] text-muted-foreground/70">
          code-grounding unavailable for: {message.codeUnavailable?.join(", ")}
        </p>
      ) : null}
    </div>
  );
}

function ChatBubble({ message }: { message: ChatMessage }) {
  if (message.role === "system") {
    // compact's summary message (per-record only) — a distinct divider.
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
        {!isUser ? <NotebookWideCitations message={message} /> : null}
      </div>
    </div>
  );
}

/** Message list + input — the SidePanelShell wrapping this owns the "chat"
 *  header bar (label + collapse chevron + ChatHeaderControls); this
 *  component renders none of that itself.
 *
 *  `mode="notebook"` adds `@`-ref autocomplete (reusing the retired ask-
 *  bar's mention logic) and the suggested-question empty state; `notes` is
 *  required in that mode (the mention search pool). `sensitive` (record
 *  mode only) shows an informational notice — the backend already refuses
 *  to auto-apply an edit on a sensitive record. */
export function ChatBody({
  state,
  mode,
  notes,
  sensitive = false,
}: {
  state: ChatPanelState;
  mode: "record" | "notebook";
  notes?: Note[];
  sensitive?: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [mentionMatchStart, setMentionMatchStart] = useState<number | null>(null);
  const [highlightedIndex, setHighlightedIndex] = useState(0);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [state.messages, state.pendingUserMessage, state.busy]);

  const mentions = state.mentions ?? [];
  const mentionMatches =
    mode === "notebook" && mentionQuery !== null && notes
      ? notes.filter((n) => n.title.toLowerCase().includes(mentionQuery.toLowerCase())).slice(0, 6)
      : [];
  const highlighted = Math.min(highlightedIndex, Math.max(mentionMatches.length - 1, 0));

  const updateMentionState = (value: string, cursorPos: number) => {
    const match = /@([^\s@]*)$/.exec(value.slice(0, cursorPos));
    if (match) {
      setMentionQuery(match[1]);
      setMentionMatchStart(cursorPos - match[0].length);
      setHighlightedIndex(0);
    } else {
      setMentionQuery(null);
      setMentionMatchStart(null);
    }
  };

  const selectMention = (note: Note) => {
    if (mentionMatchStart === null || mentionQuery === null || !state.setMentions) return;
    const before = state.input.slice(0, mentionMatchStart);
    const after = state.input.slice(mentionMatchStart + 1 + mentionQuery.length);
    state.setInput(`${before}${after}`);
    state.setMentions((prev) =>
      prev.some((m) => m.id === note.id) ? prev : [...prev, { id: note.id, title: note.title }],
    );
    setMentionQuery(null);
    setMentionMatchStart(null);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const removeMention = (id: string) =>
    state.setMentions?.((prev) => prev.filter((m) => m.id !== id));

  const isEmpty = state.messages.length === 0 && !state.pendingUserMessage;

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {sensitive ? (
        <p className="shrink-0 border-b border-amber-500/30 bg-amber-500/5 px-2 py-1 text-[9px] text-amber-400">
          🔒 sensitive {mode === "record" ? "record" : "note"} — answer-only, edits are disabled
        </p>
      ) : null}

      <div ref={scrollRef} className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto p-2">
        {isEmpty ? (
          <div className="flex h-full flex-col items-center justify-center gap-1.5 p-4 text-center">
            <Sparkles className="h-4 w-4 text-muted-foreground/50" />
            {mode === "notebook" ? (
              <>
                <p className="text-[10px] text-muted-foreground/60">
                  ask across every note, or type <code>@</code> to reference one — self-healed
                  against the live code. (One-shot per question — this conversation doesn't
                  persist across reopen.)
                </p>
                <div className="mt-1 flex flex-wrap justify-center gap-1.5">
                  {SUGGESTED_QUESTIONS.map((q) => (
                    <button
                      key={q}
                      type="button"
                      onClick={() => state.handleSend(q)}
                      className="rounded-full border border-border/70 px-2 py-1 text-[9px] text-muted-foreground transition-colors hover:border-accent-foreground/30 hover:bg-accent/40 hover:text-foreground"
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <p className="text-[10px] text-muted-foreground/60">
                ask a question, or tell it to change something — e.g. "add a section on X".
              </p>
            )}
          </div>
        ) : (
          <div className="min-w-0 space-y-2">
            {state.messages.map((m, i) => (
              <ChatBubble key={`${m.role}-${m.ts ?? i}`} message={m} />
            ))}
            {state.pendingUserMessage ? (
              <ChatBubble message={{ role: "user", content: state.pendingUserMessage }} />
            ) : null}
            {state.busy ? (
              <div className="flex items-center gap-1.5 px-1 py-1 text-[10px] text-muted-foreground">
                <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
                <span className="min-w-0 truncate">
                  {mode === "notebook"
                    ? "searching the notebook…"
                    : "researching the codebase / searching the web…"}
                </span>
              </div>
            ) : null}
            {state.jobError ? (
              <div className="flex min-w-0 items-center gap-1.5 rounded border border-destructive/40 bg-destructive/5 px-2 py-1.5 text-[10px] text-destructive">
                <AlertCircle className="h-3 w-3 shrink-0" />
                <span className="min-w-0 break-words [overflow-wrap:anywhere]">
                  {state.jobError}
                </span>
              </div>
            ) : null}
          </div>
        )}
      </div>

      <div className="shrink-0 border-t border-border/50 p-2">
        {mode === "notebook" && mentions.length > 0 ? (
          <div className="mb-1.5 flex flex-wrap gap-1">
            {mentions.map((m) => (
              <Badge key={m.id} variant="secondary" className="gap-1 py-0.5 pr-1 text-[9px]">
                <span className="max-w-[140px] truncate">@{m.title}</span>
                <button
                  type="button"
                  onClick={() => removeMention(m.id)}
                  title="Remove mention"
                  className="rounded-full p-0.5 hover:bg-background/60"
                >
                  <X className="h-2.5 w-2.5" />
                </button>
              </Badge>
            ))}
          </div>
        ) : null}
        <div className="relative flex items-center gap-1.5">
          <input
            ref={inputRef}
            value={state.input}
            onChange={(e) => {
              state.setInput(e.target.value);
              if (mode === "notebook") {
                updateMentionState(e.target.value, e.target.selectionStart ?? e.target.value.length);
              }
            }}
            onKeyDown={(e) => {
              const dropdownOpen = mode === "notebook" && mentionQuery !== null && mentionMatches.length > 0;
              if (dropdownOpen && e.key === "ArrowDown") {
                e.preventDefault();
                setHighlightedIndex((i) => (i + 1) % mentionMatches.length);
                return;
              }
              if (dropdownOpen && e.key === "ArrowUp") {
                e.preventDefault();
                setHighlightedIndex((i) => (i - 1 + mentionMatches.length) % mentionMatches.length);
                return;
              }
              if (e.key === "Enter" && !e.shiftKey) {
                if (dropdownOpen) {
                  e.preventDefault();
                  selectMention(mentionMatches[highlighted]);
                } else if (mode !== "notebook" || mentionQuery === null) {
                  e.preventDefault();
                  state.handleSend();
                }
              }
              if (e.key === "Escape" && mentionQuery !== null) {
                e.preventDefault();
                setMentionQuery(null);
                setMentionMatchStart(null);
              }
            }}
            placeholder={
              sensitive
                ? `ask a question — edits are off for sensitive ${mode === "record" ? "records" : "notes"}`
                : mode === "notebook"
                  ? "ask across all notes, or type @ to reference one…"
                  : "ask, or instruct — e.g. 'add a benchmark'"
            }
            disabled={state.busy}
            className="min-w-0 flex-1 rounded border border-border bg-background/60 px-2 py-1.5 text-xs outline-none focus:border-ring disabled:opacity-50"
          />
          <Button
            type="button"
            size="sm"
            className="h-8 shrink-0 px-2 text-[10px]"
            onClick={() => state.handleSend()}
            disabled={state.busy || !state.input.trim()}
          >
            {state.busy ? <Loader2 className="h-3 w-3 animate-spin" /> : "send"}
          </Button>
          {mode === "notebook" && mentionQuery !== null && mentionMatches.length > 0 ? (
            <div className="absolute bottom-full left-0 right-0 z-20 mb-1 max-h-40 overflow-y-auto rounded-md border border-border bg-card shadow-lg">
              {mentionMatches.map((n, i) => (
                <button
                  key={n.id}
                  type="button"
                  onClick={() => selectMention(n)}
                  onMouseEnter={() => setHighlightedIndex(i)}
                  className={cn(
                    "block w-full truncate px-2.5 py-1.5 text-left text-[11px]",
                    i === highlighted ? "bg-accent" : "hover:bg-accent/50",
                  )}
                >
                  {n.title}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
