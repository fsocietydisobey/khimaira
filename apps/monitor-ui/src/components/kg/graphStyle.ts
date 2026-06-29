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

// ---------------------------------------------------------------------------
// Palettes — a theme is a set of categorical node hues + a selection color +
// a neutral fallback. The active palette is a module singleton swapped by the
// theme picker; typeColor reads it, so the whole graph (canvas + legend +
// inspectors) recolors when it changes. Code-agnostic: still a hash → hue map,
// just over a swappable hue set.
// ---------------------------------------------------------------------------

export interface GraphPalette {
  name: string;
  label: string;
  /** Categorical node-type hues. */
  colors: readonly string[];
  /** Fallback for an empty/unknown type. */
  neutral: string;
  /** Selected/active-node highlight (NOT white — that washed out on dark). */
  selected: string;
  /** Optional 6-digit edge hue applied when this theme is picked (e.g.
   *  Wireframe wants white edges). Omit to leave the user's edge color alone. */
  edge?: string;
}

export const PALETTES: readonly GraphPalette[] = [
  {
    name: "vibrant",
    label: "Vibrant",
    colors: [
      "#0284c7",
      "#7c3aed",
      "#d97706",
      "#ea580c",
      "#059669",
      "#0d9488",
      "#e11d48",
      "#db2777",
      "#0891b2",
      "#ca8a04",
      "#4f46e5",
      "#16a34a",
      "#dc2626",
      "#9333ea",
      "#0ea5e9",
      "#65a30d",
    ],
    neutral: "#52525b",
    selected: "#fde047", // gold — pops against the vibrant field
  },
  {
    name: "neon",
    label: "Neon",
    colors: [
      "#22d3ee",
      "#a78bfa",
      "#f472b6",
      "#4ade80",
      "#facc15",
      "#fb923c",
      "#38bdf8",
      "#c084fc",
      "#2dd4bf",
      "#f87171",
      "#818cf8",
      "#fbbf24",
      "#34d399",
      "#e879f9",
      "#60a5fa",
      "#a3e635",
    ],
    neutral: "#71717a",
    selected: "#ffffff",
  },
  {
    name: "cool",
    label: "Cool",
    colors: [
      "#38bdf8",
      "#22d3ee",
      "#2dd4bf",
      "#34d399",
      "#4ade80",
      "#60a5fa",
      "#818cf8",
      "#a78bfa",
      "#67e8f9",
      "#5eead4",
      "#6ee7b7",
      "#93c5fd",
      "#7dd3fc",
      "#c084fc",
      "#a5b4fc",
      "#86efac",
    ],
    neutral: "#64748b",
    selected: "#fbbf24", // warm gold contrasts the cool field
  },
  {
    name: "warm",
    label: "Warm",
    colors: [
      "#f87171",
      "#fb923c",
      "#fbbf24",
      "#facc15",
      "#f59e0b",
      "#ef4444",
      "#fca5a5",
      "#fdba74",
      "#f472b6",
      "#e879f9",
      "#fde047",
      "#fb7185",
      "#fcd34d",
      "#ea580c",
      "#f97316",
      "#dc2626",
    ],
    neutral: "#78716c",
    selected: "#22d3ee", // cyan contrasts the warm field
  },
  {
    name: "contrast",
    label: "High contrast",
    colors: [
      "#ef4444",
      "#f59e0b",
      "#eab308",
      "#84cc16",
      "#22c55e",
      "#14b8a6",
      "#06b6d4",
      "#3b82f6",
      "#6366f1",
      "#8b5cf6",
      "#a855f7",
      "#d946ef",
      "#ec4899",
      "#f43f5e",
      "#10b981",
      "#94a3b8",
    ],
    neutral: "#475569",
    selected: "#fafafa",
  },
  {
    name: "space-gray",
    label: "Space Gray",
    // Cool, desaturated steel tones — muted "spacey" feel, but enough hue
    // spread that node types stay distinguishable on the dark canvas.
    colors: [
      "#5e9bd1",
      "#9b8fd1",
      "#5ec9c9",
      "#7fb89b",
      "#c9a15e",
      "#d18f9b",
      "#8f9bd1",
      "#6fb85e",
      "#b85e9b",
      "#5e7fb8",
      "#c9c95e",
      "#9bd15e",
      "#d1755e",
      "#b89b5e",
      "#5ed1a1",
      "#a15ed1",
    ],
    neutral: "#6b7280",
    selected: "#fbbf24", // warm amber pops against the cool muted field
  },
  {
    name: "nebula",
    label: "Nebula",
    // Cosmic violets / magentas / blues — a nebula's glow.
    colors: [
      "#a855f7",
      "#ec4899",
      "#8b5cf6",
      "#d946ef",
      "#6366f1",
      "#f472b6",
      "#7c3aed",
      "#c026d3",
      "#818cf8",
      "#e879f9",
      "#a78bfa",
      "#db2777",
      "#9333ea",
      "#f0abfc",
      "#6d28d9",
      "#fb7185",
    ],
    neutral: "#581c87",
    selected: "#5eead4", // teal cuts through the purple/pink field
  },
  {
    name: "aurora",
    label: "Aurora",
    // Greens / teals / cyans with violet accents — northern-lights palette.
    colors: [
      "#34d399",
      "#22d3ee",
      "#a78bfa",
      "#4ade80",
      "#2dd4bf",
      "#67e8f9",
      "#86efac",
      "#5eead4",
      "#818cf8",
      "#6ee7b7",
      "#38bdf8",
      "#c084fc",
      "#a3e635",
      "#7dd3fc",
      "#bef264",
      "#93c5fd",
    ],
    neutral: "#0f766e",
    selected: "#f472b6", // pink pops against the green/teal field
  },
  // --- Neural-network themes (one per reference image) ---------------------
  {
    name: "perceptron",
    label: "Perceptron",
    // MNIST-style net diagram: pale silver/white nodes on dark, faint white
    // edges, a green-yellow highlight. Mostly monochrome with subtle tints.
    colors: [
      "#e2e8f0",
      "#cbd5e1",
      "#94a3b8",
      "#aebacb",
      "#bcc6d4",
      "#8b97a8",
      "#d4dae3",
      "#9fb0c4",
      "#b0bcc9",
      "#c4ccd6",
      "#86c47f",
      "#a3b18a",
      "#dce3ea",
      "#7f8b9c",
      "#aeb8c4",
      "#cdd6e0",
    ],
    neutral: "#64748b",
    selected: "#a3e635", // the green-yellow highlight box
  },
  {
    name: "synapse",
    label: "Synapse",
    // Glowing neon nodes on deep blue — cyan/red/orange/green/magenta rings.
    colors: [
      "#22d3ee",
      "#ef4444",
      "#f97316",
      "#22c55e",
      "#d946ef",
      "#3b82f6",
      "#eab308",
      "#ec4899",
      "#14b8a6",
      "#8b5cf6",
      "#06b6d4",
      "#f43f5e",
      "#84cc16",
      "#a855f7",
      "#fb923c",
      "#10b981",
    ],
    neutral: "#1e3a8a",
    selected: "#ffffff", // a bright synaptic flash
  },
  {
    name: "connectome",
    label: "Connectome",
    // Brain connectome: the full bright spectrum scattered on black.
    colors: [
      "#3b82f6",
      "#22c55e",
      "#eab308",
      "#f97316",
      "#ef4444",
      "#06b6d4",
      "#a855f7",
      "#ec4899",
      "#14b8a6",
      "#84cc16",
      "#f59e0b",
      "#8b5cf6",
      "#10b981",
      "#0ea5e9",
      "#d946ef",
      "#fbbf24",
    ],
    neutral: "#334155",
    selected: "#ffffff",
  },
  {
    name: "deep-blue",
    label: "Deep Blue",
    // Monochrome deep-net: tonal blues + cyans, cyan-white edges on near-black.
    colors: [
      "#1d4ed8",
      "#2563eb",
      "#3b82f6",
      "#60a5fa",
      "#0ea5e9",
      "#38bdf8",
      "#7dd3fc",
      "#06b6d4",
      "#22d3ee",
      "#0891b2",
      "#0e7490",
      "#155e75",
      "#1e40af",
      "#93c5fd",
      "#67e8f9",
      "#0284c7",
    ],
    neutral: "#1e3a8a",
    selected: "#fde047", // gold cuts through the all-blue field
  },
  {
    name: "cortex",
    label: "Cortex",
    // Cortex scan: cyan-blue web with pink/magenta/red highlights.
    colors: [
      "#38bdf8",
      "#22d3ee",
      "#ec4899",
      "#0ea5e9",
      "#f472b6",
      "#06b6d4",
      "#ef4444",
      "#60a5fa",
      "#db2777",
      "#7dd3fc",
      "#fb7185",
      "#3b82f6",
      "#e879f9",
      "#67e8f9",
      "#f43f5e",
      "#818cf8",
    ],
    neutral: "#1e293b",
    selected: "#fef08a",
  },
  {
    name: "dendrite",
    label: "Dendrite",
    // Neuron tracing: blue/cyan dendrites with orange/red/green axons on black.
    colors: [
      "#38bdf8",
      "#0ea5e9",
      "#f97316",
      "#22c55e",
      "#22d3ee",
      "#ef4444",
      "#60a5fa",
      "#84cc16",
      "#06b6d4",
      "#fb923c",
      "#10b981",
      "#7dd3fc",
      "#f59e0b",
      "#3b82f6",
      "#dc2626",
      "#67e8f9",
    ],
    neutral: "#0c4a6e",
    selected: "#fde047",
  },
  {
    name: "pyramidal",
    label: "Pyramidal",
    // Pyramidal-neuron diagram: royal blue → periwinkle → indigo → lavender
    // cell bodies + dendrites on near-black. A blue-violet monochrome.
    colors: [
      "#4f6ef7",
      "#818cf8",
      "#6366f1",
      "#a5b4fc",
      "#5b6ee1",
      "#7c83eb",
      "#93a0f5",
      "#4338ca",
      "#6d7bf0",
      "#8b9bf0",
      "#5468d4",
      "#a0acf8",
      "#4c5fd7",
      "#7986e8",
      "#b4bdfb",
      "#5d6ae6",
    ],
    neutral: "#312e81",
    selected: "#fbbf24", // warm amber cuts through the blue-violet field
  },
  {
    name: "wireframe",
    label: "Wireframe",
    // All-white / silver nodes + white edges on near-black — the glowing
    // monochrome neural-net wireframe look. Subtle gray variation keeps node
    // types faintly distinguishable; the `edge` hue makes the whole web white.
    colors: [
      "#f8fafc",
      "#e2e8f0",
      "#cbd5e1",
      "#f1f5f9",
      "#d6dde6",
      "#eef2f6",
      "#c4cdd8",
      "#dde4ec",
      "#e8edf2",
      "#d0d8e2",
      "#f3f6f9",
      "#c9d2dd",
      "#e0e6ed",
      "#d8e0e8",
      "#edf1f5",
      "#cdd6e0",
    ],
    neutral: "#94a3b8",
    selected: "#22d3ee", // cyan is the one hue that pops against all-white
    edge: "#293b4c", // dark slate-blue edges (the default)
  },
];

const DEFAULT_PALETTE = PALETTES[0];

let _active: GraphPalette = DEFAULT_PALETTE;

/** Swap the active palette by name (no-op on an unknown name). */
export function setActivePalette(name: string): void {
  const next = PALETTES.find((p) => p.name === name) ?? _active;
  if (next !== _active) {
    _active = next;
    // The type→color registry is palette-bound — invalidate so it rebuilds
    // against the new palette on the next registerTypes() call.
    _typeColorRegistry.clear();
    _registryPaletteName = "";
  }
}

/** Name of the currently active palette. */
export function activePaletteName(): string {
  return _active.name;
}

/** Selected/active-node highlight color for the active palette. */
export function selectedColor(): string {
  return _active.selected;
}

/** Stable 32-bit string hash (djb2-ish). */
function hashString(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i += 1) {
    h = (h << 5) - h + s.charCodeAt(i);
    h |= 0; // force 32-bit
  }
  return Math.abs(h);
}

// ---------------------------------------------------------------------------
// Perceptually-distinct type → color assignment.
//
// The old `palette[hashString(type) % len]` could map two DISTINCT types to the
// SAME color (hash collision mod palette size). Round 1 fixed exact-hex collisions
// by assigning curated-palette colors by sorted ordinal — but the curated palettes
// are perceptually CLUSTERED (distinct slots, near-identical hues: e.g. 3 ambers,
// 3 greens), so types still read as "duplicate" to the eye.
//
// Round 2: spread every type an EVENLY-SPACED hue (`ordinal*360/N`) for max
// separation — but that flattened every theme to the SAME full-wheel rainbow (only
// S/L differed), erasing theme identity.
//
// Round 3 "balanced" (Joseph's pick): give each theme its CHARACTER back while keeping
// distinctness. Derive the theme's hue character from its curated palette (circular
// stats → `_paletteHueProfile` = {center, arc}); spread the N type hues evenly across
// that arc CENTERED on the theme's center, but widen the arc to at least `N*MIN_GAP`
// so adjacent types stay ≥ MIN_GAP apart. Net: few types → tight, strongly theme-tinted
// spread; many types (13) → arc widens toward the full wheel so the gap floor holds
// (distinctness wins, as accepted). Vibrant ≈ full spread; Cool leans cool; Warm warm.
// S/L still come from the theme's vibrancy profile (`_paletteSL`, unchanged).
//
// `registerTypes(presentTypes)` (called by KgMapper, which has the full node_types
// set) builds the registry; `typeColor` reads it.
// ---------------------------------------------------------------------------

const _typeColorRegistry: Map<string, string> = new Map();
let _registryPaletteName = "";

/** HSL → #rrggbb (so generated colors parse in typeColorAlpha like palette hexes). */
function hslToHex(h: number, s: number, l: number): string {
  const sN = s / 100;
  const lN = l / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sN * Math.min(lN, 1 - lN);
  const f = (n: number) => {
    const color = lN - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return Math.round(255 * color)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

/** #rrggbb → S,L in 0–100 (hue is irrelevant for the theme S/L profile). */
function hexToSL(hex: string): { s: number; l: number } {
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  let s = 0;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  }
  return { s: s * 100, l: l * 100 };
}

/** #rrggbb → hue in [0,360). Grey (max===min) → 0. */
function hexToHue(hex: string): number {
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  if (d === 0) return 0;
  let h: number;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  h *= 60;
  return h < 0 ? h + 360 : h;
}

// The active theme's "vibrancy profile" — average S/L of its curated colors.
// Cached per palette name (the curated arrays are immutable).
const _paletteSLCache: Map<string, { s: number; l: number }> = new Map();
function _paletteSL(palette: GraphPalette): { s: number; l: number } {
  const cached = _paletteSLCache.get(palette.name);
  if (cached) return cached;
  let sSum = 0;
  let lSum = 0;
  for (const hex of palette.colors) {
    const { s, l } = hexToSL(hex);
    sSum += s;
    lSum += l;
  }
  const n = palette.colors.length || 1;
  const profile = { s: Math.round(sSum / n), l: Math.round(lSum / n) };
  _paletteSLCache.set(palette.name, profile);
  return profile;
}

// The active theme's "hue character" — where on the wheel its curated colors sit
// (center) and how wide a band they span (arc), via CIRCULAR statistics. A clustered
// palette (Cool ~ blues) → small arc around its center; a full-wheel palette (Vibrant)
// → arc ≈ 360. Cached per palette name. Round 3 "balanced" (2026-06-29): the type
// colors lean toward this character (theme identity) while a MIN_GAP floor preserves
// distinctness — replacing round 2's flat full-wheel rainbow that erased theme identity.
const _paletteHueCache: Map<string, { center: number; arc: number }> = new Map();
function _paletteHueProfile(palette: GraphPalette): { center: number; arc: number } {
  const cached = _paletteHueCache.get(palette.name);
  if (cached) return cached;
  const hues = palette.colors.map(hexToHue);
  // Circular mean: average unit vectors, atan2 → mean angle (handles 350°/10° → 0°,
  // NOT 180°). Resultant ~0 for a full-wheel palette → center is arbitrary, but its
  // arc ≈ 360 covers the wheel anyway so the center barely matters there.
  let x = 0;
  let y = 0;
  for (const h of hues) {
    x += Math.cos((h * Math.PI) / 180);
    y += Math.sin((h * Math.PI) / 180);
  }
  let center = (Math.atan2(y, x) * 180) / Math.PI;
  if (center < 0) center += 360;
  // Circular range = 360 − the largest empty gap between consecutive sorted hues.
  // Full-wheel palette → small max gap → arc ≈ 360; clustered → big empty gap → small arc.
  const sorted = [...hues].sort((a, b) => a - b);
  let maxGap = 0;
  for (let i = 0; i < sorted.length; i += 1) {
    const next = sorted[(i + 1) % sorted.length];
    const gap = (next - sorted[i] + 360) % 360;
    if (gap > maxGap) maxGap = gap;
  }
  const arc = sorted.length > 1 ? 360 - maxGap : 0;
  const profile = { center, arc };
  _paletteHueCache.set(palette.name, profile);
  return profile;
}

// Distinctness floor: adjacent type colors are kept ≥ this many degrees apart on the
// wheel. Tunes the theme-character ↔ distinctness balance (lower = more theme tint,
// tighter spacing). With N types the spread arc widens to at least N*MIN_GAP.
const _MIN_HUE_GAP = 20;

/** "Balanced" color for ordinal `i` of `total` distinct types: hues are spread
 *  evenly across the theme's hue arc (CENTERED on its hue character) but the arc is
 *  widened to at least `total * MIN_GAP` so adjacent types stay distinguishable.
 *  S/L come from the theme's vibrancy profile. Few types → tight, strongly tinted
 *  spread; many types → arc widens toward the full wheel (distinctness wins). */
function _colorForOrdinal(i: number, total: number): string {
  const { s, l } = _paletteSL(_active);
  if (total <= 0) return hslToHex(0, s, l);
  const { center, arc } = _paletteHueProfile(_active);
  const effArc = Math.min(360, Math.max(arc, total * _MIN_HUE_GAP));
  const hue = (((center - effArc / 2 + (i + 0.5) * (effArc / total)) % 360) + 360) % 360;
  return hslToHex(hue, s, l);
}

/** Register the full set of present node types so `typeColor` can hand each a
 *  collision-free color. Idempotent + cheap; rebuilds against the active palette.
 *  KgMapper calls this with its sorted `presentTypes` set each render. */
export function registerTypes(types: readonly string[]): void {
  const distinct = Array.from(new Set(types.filter(Boolean))).sort();
  // Skip the rebuild when nothing changed (same set + same palette this pass).
  const sameSet =
    _registryPaletteName === _active.name &&
    distinct.length === _typeColorRegistry.size &&
    distinct.every((t) => _typeColorRegistry.has(t));
  if (sameSet) return;
  _typeColorRegistry.clear();
  distinct.forEach((t, i) => {
    _typeColorRegistry.set(t, _colorForOrdinal(i, distinct.length));
  });
  _registryPaletteName = _active.name;
}

/** Deterministic, collision-free color for an opaque `type` string (active
 *  palette). Reads the registry built by `registerTypes`; an unregistered type
 *  (e.g. an edge type not in the node_types set) falls back to the legacy hash. */
export function typeColor(type: string): string {
  if (!type) return _active.neutral;
  const registered = _typeColorRegistry.get(type);
  if (registered) return registered;
  return _active.colors[hashString(type) % _active.colors.length];
}

/** `typeColor` as an `rgba(...)` string at the given alpha (0–1). */
export function typeColorAlpha(type: string, alpha: number): string {
  const hex = typeColor(type);
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// ---------------------------------------------------------------------------
// Edge styling. The DEFAULT edge look is a neutral faint slate web — on a dense
// graph (9970 edges) coloring edges by type is just noise; node color already
// carries type, and the neutral web reveals structure far better. So edges are
// neutral always, and the confidence overlay only RECOLORS the suspect
// (low-weight) ones on top of that same neutral base.
// ---------------------------------------------------------------------------

/** Default edge color — dark slate-blue (RGB 41,59,76). */
export const EDGE_BASE_COLOR = "#293b4c33";
/** Default edge thickness — very thin, so dense hubs read as fine filaments
 *  instead of blowing out into a solid blob (the wireframe look). */
export const EDGE_BASE_SIZE = 0.28;
/** Default 6-digit edge hue for the edge color picker (EDGE_BASE_COLOR's hue). */
export const EDGE_BASE_HUE = "#293b4c";
/** Alpha suffix appended to the picked edge hue — keeps the web faint. */
export const EDGE_ALPHA = "33";

/** Confidence band for an edge weight: low (<0.7), mid (<0.9), or high. */
export function confidenceBand(weight: number): "low" | "mid" | "high" {
  if (weight < 0.7) return "low";
  if (weight < 0.9) return "mid";
  return "high";
}

/** Edge color in confidence-encoding mode — low confidence is loud; high
 *  confidence stays on the neutral base (identical to the default look). */
export function confidenceColor(weight: number): string {
  switch (confidenceBand(weight)) {
    case "low":
      return "#ef4444cc"; // red — suspect, demands a look
    case "mid":
      return "#f59e0b99"; // amber — worth a glance
    default:
      return EDGE_BASE_COLOR; // trusted — same neutral web as the default
  }
}

/** Edge thickness in confidence-encoding mode — low confidence is thicker. */

/** Edge thickness in confidence-encoding mode — low confidence is thicker. */
export function confidenceSize(weight: number): number {
  switch (confidenceBand(weight)) {
    case "low":
      return 2.6;
    case "mid":
      return 1.3;
    default:
      return 0.4;
  }
}
