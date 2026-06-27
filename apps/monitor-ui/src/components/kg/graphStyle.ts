/**
 * graphStyle — deterministic, code-agnostic styling for the generic graph
 * contract.
 *
 * The KG viewer must render ANY attached project's graph (mirroring how
 * FlowCanvas renders any LangGraph project), so a node/edge `type` is an
 * OPAQUE string — there is no fixed enum of jeevy node/link types. We map an
 * arbitrary type string to a stable color by hashing it into a fixed palette.
 * Same type → same color, every render, for any project. No per-domain map.
 */

/** Fixed categorical palette (mid-tone hexes; white text reads on all). */
const PALETTE: readonly string[] = [
  "#0284c7", // sky
  "#7c3aed", // violet
  "#d97706", // amber
  "#ea580c", // orange
  "#059669", // emerald
  "#0d9488", // teal
  "#e11d48", // rose
  "#db2777", // pink
  "#0891b2", // cyan
  "#ca8a04", // yellow
  "#4f46e5", // indigo
  "#16a34a", // green
  "#dc2626", // red
  "#9333ea", // purple
  "#0ea5e9", // light-blue
  "#65a30d", // lime
];

/** Neutral fallback for an empty/unknown type. */
const NEUTRAL = "#52525b";

/** Stable 32-bit string hash (djb2-ish). */
function hashString(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i += 1) {
    h = (h << 5) - h + s.charCodeAt(i);
    h |= 0; // force 32-bit
  }
  return Math.abs(h);
}

/** Deterministic color for an opaque `type` string. */
export function typeColor(type: string): string {
  if (!type) return NEUTRAL;
  return PALETTE[hashString(type) % PALETTE.length];
}

/** `typeColor` as an `rgba(...)` string at the given alpha (0–1). */
export function typeColorAlpha(type: string, alpha: number): string {
  const hex = typeColor(type);
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
