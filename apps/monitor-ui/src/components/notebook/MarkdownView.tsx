/**
 * MarkdownView — the ONE shared renderer for every note text surface:
 * summary/technical/plain/organized_md, the original/raw_text view, and the
 * draft "processing" fallback. Joseph's notes are markdown-with-mermaid
 * (headings, inline code, lists, GFM tables, ```mermaid fences), so this
 * needs GFM support + a mermaid-aware code renderer.
 *
 * Overflow containment (the KG node-inspector fix, commit 70337e8, mirrored
 * here): a wide GFM table or code block must never widen the note card.
 * Prose wraps via overflow-wrap; tables/code get their OWN overflow-x:auto
 * wrapper so THEY scroll internally instead of pushing the card wider. The
 * root here is `min-w-0` — callers must also put `min-w-0` on the card/grid
 * item, since a flex/grid child's default min-width:auto is what lets a
 * wide descendant force the container wider in the first place.
 */

import { lazy, Suspense, useRef } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Lazy-loaded — mermaid.js is a sizeable chunk most notes never touch. Only
// fetched the first time a note actually contains a ```mermaid fence.
const MermaidBlock = lazy(() =>
  import("@/components/notebook/MermaidBlock").then((m) => ({ default: m.MermaidBlock })),
);

/** Flatten a heading's children into plain text, for slug generation. Headings
 *  can contain inline code/emphasis, so this isn't just `props.children`. */
function headingText(node: ReactNode): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(headingText).join("");
  if (node && typeof node === "object" && "props" in node) {
    return headingText((node as { props: { children?: ReactNode } }).props.children);
  }
  return "";
}

/** GitHub-style heading slug (lowercase, strip punctuation, spaces→hyphens,
 *  de-dupe with a -N suffix) — the common convention for a "deterministic
 *  heading parse". Grimoire (Phase 1f): must match whatever anchor scheme
 *  `notebook_pipeline._parse_toc` (backend) produces for a guide's TOC —
 *  if the backend's contract differs, this is the one place to adjust. */
function slugifyHeading(text: string, seen: Map<string, number>): string {
  const base =
    text
      .toLowerCase()
      .trim()
      .replace(/[^\w\- ]+/g, "")
      .replace(/\s+/g, "-") || "section";
  const count = seen.get(base) ?? 0;
  seen.set(base, count + 1);
  return count === 0 ? base : `${base}-${count}`;
}

export function MarkdownView({
  content,
  slugHeadings = false,
}: {
  content: string;
  /** Grimoire (Phase 1f): assign each heading a stable `id` (GitHub-slug of
   *  its text) so a guide's TOC can scroll-to-anchor. Off by default — every
   *  other MarkdownView surface (summary/technical/plain/answers) doesn't
   *  need heading ids and shouldn't pay the slug bookkeeping. */
  slugHeadings?: boolean;
}) {
  // Reset once per render — headings are recomputed fresh every render (cheap
  // relative to markdown parsing itself), so dedup counts never leak stale
  // state across a content change.
  const seenSlugs = useRef<Map<string, number>>(new Map());
  seenSlugs.current = new Map();

  const headingId = (children: ReactNode): string | undefined =>
    slugHeadings ? slugifyHeading(headingText(children), seenSlugs.current) : undefined;

  return (
    <div className="min-w-0 max-w-full overflow-x-hidden text-xs">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: (props) => (
            <h1
              id={headingId(props.children)}
              className="mb-1.5 mt-3 text-sm font-semibold text-foreground first:mt-0"
              {...props}
            />
          ),
          h2: (props) => (
            <h2
              id={headingId(props.children)}
              className="mb-1.5 mt-3 text-[13px] font-semibold text-foreground first:mt-0"
              {...props}
            />
          ),
          h3: (props) => (
            <h3
              id={headingId(props.children)}
              className="mb-1 mt-2 text-xs font-semibold text-foreground first:mt-0"
              {...props}
            />
          ),
          h4: (props) => (
            <h4
              id={headingId(props.children)}
              className="mb-1 mt-2 text-xs font-semibold text-foreground first:mt-0"
              {...props}
            />
          ),
          p: (props) => (
            <p
              className="mb-2 leading-relaxed text-muted-foreground [overflow-wrap:anywhere] last:mb-0"
              {...props}
            />
          ),
          ul: (props) => (
            <ul className="mb-2 list-disc space-y-0.5 pl-4 text-muted-foreground" {...props} />
          ),
          ol: (props) => (
            <ol className="mb-2 list-decimal space-y-0.5 pl-4 text-muted-foreground" {...props} />
          ),
          li: (props) => <li className="[overflow-wrap:anywhere]" {...props} />,
          a: (props) => (
            <a
              className="text-sky-400 underline underline-offset-2 hover:text-sky-300"
              target="_blank"
              rel="noreferrer"
              {...props}
            />
          ),
          strong: (props) => <strong className="font-semibold text-foreground" {...props} />,
          blockquote: (props) => (
            <blockquote
              className="mb-2 border-l-2 border-border pl-2 italic text-muted-foreground/80"
              {...props}
            />
          ),
          hr: (props) => <hr className="my-3 border-border" {...props} />,
          table: (props) => (
            <div className="mb-2 min-w-0 overflow-x-auto rounded border border-border">
              <table className="w-full min-w-max text-left text-[11px]" {...props} />
            </div>
          ),
          thead: (props) => <thead className="bg-card/60" {...props} />,
          th: (props) => (
            <th
              className="whitespace-nowrap border-b border-border px-2 py-1 font-medium text-foreground"
              {...props}
            />
          ),
          td: (props) => (
            <td
              className="whitespace-nowrap border-b border-border/50 px-2 py-1 text-muted-foreground"
              {...props}
            />
          ),
          // react-markdown always wraps fenced code as <pre><code>; since our
          // `code` override renders its own <pre>/diagram box, `pre` here is
          // a passthrough so we don't end up with <pre><pre>...</pre></pre>.
          pre: (props) => <>{props.children}</>,
          code(props) {
            const { className, children, ...rest } = props;
            const isBlock = Boolean(className); // fenced code only — inline code has no className
            const lang = /language-(\w+)/.exec(className ?? "")?.[1];
            const text = String(children).replace(/\n$/, "");

            if (isBlock && lang === "mermaid") {
              return (
                <Suspense
                  fallback={
                    <pre className="mb-2 min-w-0 overflow-x-auto rounded bg-background/60 p-2 text-[11px]">
                      <code>{text}</code>
                    </pre>
                  }
                >
                  <MermaidBlock code={text} />
                </Suspense>
              );
            }
            if (isBlock) {
              return (
                <pre className="mb-2 min-w-0 overflow-x-auto rounded bg-background/60 p-2 text-[11px]">
                  <code className={className} {...rest}>
                    {children}
                  </code>
                </pre>
              );
            }
            return (
              <code
                className="rounded bg-background/60 px-1 py-0.5 text-[11px] [overflow-wrap:anywhere]"
                {...rest}
              >
                {children}
              </code>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
