/**
 * JsonTree — recursive collapsible JSON renderer.
 *
 * Ported from jeevy_portal/frontend/src/features/ai-debugger/views/langgraph/JsonTree.js
 * (re-port at every chimera-monitor phase boundary per locked decision 2026-05-06).
 *
 * Drift-checked by `scripts/check_jeevy_drift.py`.
 */

import { useState } from "react";

interface ValueProps {
  value: unknown;
}

function JsonValue({ value }: ValueProps) {
  if (value === null) return <span className="jsonNull">null</span>;
  if (typeof value === "boolean") return <span className="jsonBool">{String(value)}</span>;
  if (typeof value === "number") return <span className="jsonNumber">{value}</span>;
  if (typeof value === "string") return <span className="jsonString">"{value}"</span>;
  return <span>{String(value)}</span>;
}

interface BranchProps {
  keyName: string | number | null;
  value: unknown;
  topLevel: boolean;
}

function JsonBranch({ keyName, value, topLevel }: BranchProps) {
  const isObject = value !== null && typeof value === "object" && !Array.isArray(value);
  const isArray = Array.isArray(value);
  const isContainer = isObject || isArray;
  const [collapsed, setCollapsed] = useState(!topLevel);

  const keyPart =
    keyName != null ? (
      <>
        <span className="jsonKey">"{String(keyName)}"</span>
        <span className="jsonBracket">: </span>
      </>
    ) : null;

  if (!isContainer) {
    return (
      <>
        {keyPart}
        <JsonValue value={value} />
      </>
    );
  }

  const entries: Array<[string | number, unknown]> = isArray
    ? (value as unknown[]).map((v, i): [number, unknown] => [i, v])
    : Object.keys(value as Record<string, unknown>).map((k): [string, unknown] => [
        k,
        (value as Record<string, unknown>)[k],
      ]);

  if (entries.length === 0) {
    return (
      <>
        {keyPart}
        <span className="jsonBracket">{isArray ? "[]" : "{}"}</span>
      </>
    );
  }

  const open = isArray ? "[" : "{";
  const close = isArray ? "]" : "}";

  return (
    <>
      {keyPart}
      <span
        className="jsonToggle"
        onClick={() => setCollapsed((c) => !c)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setCollapsed((c) => !c);
          }
        }}
      >
        <span className="jsonBracket">{open}</span>{" "}
        <span className="jsonCount">{entries.length}</span>{" "}
        <span className="jsonBracket">{close}</span>
      </span>
      {!collapsed ? (
        <div className="jsonChildren">
          {entries.map(([k, v], idx) => (
            <div key={isArray ? idx : String(k)}>
              <JsonBranch keyName={isArray ? null : k} value={v} topLevel={false} />
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}

interface JsonTreeProps {
  data: unknown;
}

export function JsonTree({ data }: JsonTreeProps) {
  if (data === null || typeof data !== "object") {
    return <div className="jsonTree"><JsonValue value={data} /></div>;
  }
  const keys = Array.isArray(data)
    ? (data as unknown[]).map((_, i) => i)
    : Object.keys(data as Record<string, unknown>).sort();

  return (
    <div className="jsonTree">
      {keys.map((k) => (
        <div key={String(k)}>
          <JsonBranch
            keyName={Array.isArray(data) ? null : (k as string)}
            value={(data as Record<string | number, unknown>)[k as string]}
            topLevel
          />
        </div>
      ))}
    </div>
  );
}
