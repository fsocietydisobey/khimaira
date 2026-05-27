"""Core sync logic: manifest → role docs + Themis YAML + knowledge seeds.

``khimaira leads sync <project_root>`` calls ``sync_leads()``.
``khimaira leads sync --check <project_root>`` calls ``check_drift()``.
"""

from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path

from .glob_to_regex import build_allow_regex
from .manifest import Manifest, load_manifest

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_TEMPLATE_ROLE = _TEMPLATES_DIR / "lead-role.md.j2"
_TEMPLATE_THEMIS = _TEMPLATES_DIR / "lead-themis.yaml.j2"

_MANUAL_BLOCK_RE = re.compile(
    r"(<!-- BEGIN MANUAL -->.*?<!-- END MANUAL -->)",
    re.DOTALL,
)


def _relative_or_absolute(path: Path, root: Path) -> str:
    """Return path relative to root if under it; absolute POSIX string otherwise.

    Handles the cross-repo case: when output dirs (roles_dir, knowledge_dir)
    live in a different repo from root_path (e.g. jeevy leads whose docs land
    in the khimaira repo), .relative_to() raises ValueError. Fall back to the
    absolute path so templates and allow-regexes remain correct.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def _render(template_text: str, vars: dict[str, str]) -> str:
    """Render a ``{{ var }}`` template — simple substitution, no jinja2.

    Raises KeyError if a variable appears in the template but is not supplied.
    """

    def _replace(m: re.Match) -> str:
        key = m.group(1).strip()
        if key not in vars:
            raise KeyError(f"Template variable '{{{{{key}}}}}' not provided")
        return vars[key]

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _replace, template_text)


def _extract_manual_blocks(content: str) -> list[str]:
    """Return all ``<!-- BEGIN MANUAL -->...<!-- END MANUAL -->`` blocks."""
    return _MANUAL_BLOCK_RE.findall(content)


def _preserve_manual_blocks(generated: str, existing_blocks: list[str]) -> str:
    """Replace manual-block placeholders in ``generated`` with ``existing_blocks``.

    Blocks are matched positionally — the N-th block in ``generated`` is replaced
    by the N-th block in ``existing_blocks``. Extra existing blocks are dropped;
    missing existing blocks leave the generated block unchanged.
    """
    it = iter(existing_blocks)

    def _replace_block(m: re.Match) -> str:
        try:
            return next(it)
        except StopIteration:
            return m.group(0)

    return _MANUAL_BLOCK_RE.sub(_replace_block, generated)


# ---------------------------------------------------------------------------
# Per-lead generation
# ---------------------------------------------------------------------------


def _lead_name(domain: str, prefix: str) -> str:
    """Derive the canonical lead name: ``[<prefix>-]<domain>-lead``.

    Prefix and domain are orthogonal axes:
    - prefix = which roster (session-name scoping), e.g. "jp" for jeevy
    - domain = what tech area, always bare, e.g. "backend" (same across rosters)

    Examples:
    - khimaira (prefix=""): ``backend-lead``
    - jeevy   (prefix="jp"): ``jp-backend-lead``

    Domain stays bare in knowledge docs and mnemosyne keys for cross-project
    consistency. Prefix applies ONLY to session-name-derived artifacts (role doc
    filename, Themis rule ID).
    """
    if prefix:
        return f"{prefix}-{domain}-lead"
    return f"{domain}-lead"


def _themis_rule_ids(domain: str, prefix: str) -> tuple[str, str]:
    """Return (rule_id_1, rule_id_2) for a lead's two Themis invariants.

    Convention: ``IN-[<PREFIX>-]<DOMAIN>-LEAD-1`` / ``…-LEAD-2`` (uppercased).
    Examples: khimaira backend → ``IN-BACKEND-LEAD-1``; jeevy → ``IN-JP-BACKEND-LEAD-1``.
    """
    parts = [p.upper() for p in ([prefix, domain] if prefix else [domain])]
    base = "-".join(parts)
    return f"IN-{base}-LEAD-1", f"IN-{base}-LEAD-2"


def _propose_only_rule_block(
    themis_rule_id: str,
    lead_name: str,
    project_name: str,
) -> str:
    """Return the YAML block for the propose-only invariant, or '' if not needed."""
    po_id = f"{themis_rule_id}-PO"
    return (
        f"\n  - id: {po_id}\n"
        f"    name: NO_FILE_EDIT_PROPOSE_ONLY\n"
        f"    severity: block\n"
        f"    matchers:\n"
        f"      - tool: Edit\n"
        f"      - tool: Write\n"
        f"      - tool: MultiEdit\n"
        f"      - tool: NotebookEdit\n"
        f"    message: |\n"
        f"      \U0001f6d1 Themis {po_id} (NO_FILE_EDIT_PROPOSE_ONLY): {lead_name} is in\n"
        f"      PROPOSE-ONLY mode for {project_name}. Audit, analyze, propose, and\n"
        f"      review only — no writes without explicit Joseph authorization via\n"
        f"      intake/master. To implement, propose your plan to master; master\n"
        f"      dispatches to an authorized agent or grants explicit write permission.\n"
    )


def _propose_only_how_to_work_block(
    themis_rule_id: str,
) -> str:
    """Return the propose-only how-to-work section, or '' if not needed."""
    po_id = f"{themis_rule_id}-PO"
    return (
        f"\n## 🛠 How You Work — PROPOSE-ONLY mode\n\n"
        f"Because this roster is `propose_only`, you are the **domain authority\n"
        f"but NOT the executor**. Themis blocks all writes; master's implementing\n"
        f"agent is your hands.\n\n"
        f"**Propose-only workflow:**\n\n"
        f"1. Receive intent from master (same as standard flow).\n"
        f"2. Read knowledge first (same as standard flow).\n"
        f"3. **Produce an IMPLEMENTATION-READY plan** — concrete file paths, exact\n"
        f"   changes, acceptance criteria. Do NOT attempt to execute; Themis blocks\n"
        f"   writes anyway ({po_id} — NO_FILE_EDIT_PROPOSE_ONLY).\n"
        f"4. **Send plan to master** via `chat_send_to`. Master dispatches an\n"
        f"   implementing agent with your plan as the spec.\n"
        f"5. **Guide the implementing agent.** Answer its domain questions; review\n"
        f"   its output against your plan; flag domain-correctness issues to master.\n"
        f"6. **You are the domain authority; the agent is your hands.** This\n"
        f"   lead↔agent guidance is allowed — the agent executes YOUR plan, which\n"
        f"   is NOT the forbidden cross-lead peer-coordination.\n"
    )


def _propose_only_section_block(
    themis_rule_id: str,
    project_name: str,
) -> str:
    """Return the role-doc PROPOSE-ONLY constraint section, or '' if not needed."""
    po_id = f"{themis_rule_id}-PO"
    return (
        f"- **PROPOSE-ONLY in {project_name}** ⚠️  This lead may NOT edit files in\n"
        f"  {project_name}. Write access requires explicit Joseph authorization via\n"
        f"  intake/master. Correct workflow: analyze → propose a plan via `chat_send_to`\n"
        f"  to master → master dispatches implementation to an agent or grants explicit\n"
        f"  write permission. **This constraint OVERRIDES the global small-plans clause**\n"
        f"  (step 5 above). Even 1-file edits require master approval here.\n"
        f"  **Enforcement:** {po_id} (NO_FILE_EDIT_PROPOSE_ONLY) — Themis hard-block.\n"
    )


def _render_role_doc(
    manifest: Manifest,
    domain: str,
    lead_cfg,
    existing_content: str | None = None,
) -> str:
    """Render the role doc for one lead, preserving any manual blocks."""
    lead_name = _lead_name(domain, manifest.prefix)
    knowledge_doc_path = _relative_or_absolute(
        manifest.knowledge_dir / f"{domain}-knowledge.md",
        manifest.root_path,
    )
    themis_rule_id, themis_rule_id_2 = _themis_rule_ids(domain, manifest.prefix)

    paths_list = "\n".join(f"- `{p}`" for p in lead_cfg.paths)

    propose_only_section = (
        _propose_only_section_block(themis_rule_id, manifest.project_name)
        if manifest.propose_only
        else ""
    )
    propose_only_how_to_work = (
        _propose_only_how_to_work_block(themis_rule_id)
        if manifest.propose_only
        else ""
    )

    template_text = _TEMPLATE_ROLE.read_text()
    rendered = _render(
        template_text,
        {
            "domain": domain,
            "Domain": domain.capitalize(),
            "DOMAIN": domain.upper(),
            "lead_name": lead_name,
            "roster": manifest.project_name,
            "model": lead_cfg.model,
            "Model": lead_cfg.model.capitalize(),
            "effort": lead_cfg.effort,
            "knowledge_doc_path": knowledge_doc_path,
            "paths_list": paths_list,
            "themis_rule_id": themis_rule_id,
            "themis_rule_id_2": themis_rule_id_2,
            "propose_only_section": propose_only_section,
            "propose_only_how_to_work": propose_only_how_to_work,
        },
    )

    if existing_content:
        existing_blocks = _extract_manual_blocks(existing_content)
        if existing_blocks:
            rendered = _preserve_manual_blocks(rendered, existing_blocks)

    return rendered


def _render_themis_yaml(
    manifest: Manifest,
    domain: str,
    lead_cfg,
) -> str:
    """Render the Themis YAML for one lead."""
    lead_name = _lead_name(domain, manifest.prefix)
    knowledge_doc_path = _relative_or_absolute(
        manifest.knowledge_dir / f"{domain}-knowledge.md",
        manifest.root_path,
    )
    role_doc_path = _relative_or_absolute(
        manifest.roles_dir / f"{lead_name}.md",
        manifest.root_path,
    )
    themis_rule_id, themis_rule_id_2 = _themis_rule_ids(domain, manifest.prefix)

    allow_regex = build_allow_regex(
        paths=lead_cfg.paths,
        role_doc_path=role_doc_path,
        knowledge_doc_path=knowledge_doc_path,
    )

    propose_only_rule = (
        _propose_only_rule_block(themis_rule_id, lead_name, manifest.project_name)
        if manifest.propose_only
        else ""
    )

    template_text = _TEMPLATE_THEMIS.read_text()
    rendered = _render(
        template_text,
        {
            "lead_name": lead_name,
            "domain": domain,
            "Domain": domain.capitalize(),
            "DOMAIN": domain.upper(),
            "themis_rule_id": themis_rule_id,
            "themis_rule_id_2": themis_rule_id_2,
            "allow_regex": allow_regex,
            "propose_only_rule": propose_only_rule,
        },
    )
    # Normalize: exactly one trailing newline regardless of propose_only_rule content.
    return rendered.rstrip() + "\n"


# ---------------------------------------------------------------------------
# Knowledge seed
# ---------------------------------------------------------------------------

def _seed_knowledge_doc(
    manifest: Manifest,
    domain: str,
) -> tuple[Path, bool]:
    """Seed <knowledge_dir>/<domain>-knowledge.md if it doesn't exist.

    Returns (path, was_created).  Never clobbers an existing file.
    """
    knowledge_path = manifest.knowledge_dir / f"{domain}-knowledge.md"
    if knowledge_path.exists():
        return knowledge_path, False

    # Find the template relative to the project root
    template_candidates = [
        manifest.knowledge_dir / "_template-knowledge.md",
        manifest.knowledge_dir.parent / "_template-knowledge.md",
    ]
    template_src = None
    for candidate in template_candidates:
        if candidate.exists():
            template_src = candidate
            break

    knowledge_path.parent.mkdir(parents=True, exist_ok=True)

    if template_src:
        shutil.copy(template_src, knowledge_path)
    else:
        # Fallback: minimal header
        knowledge_path.write_text(
            f"# {domain.capitalize()} Domain Knowledge — {manifest.project_name}\n\n"
            "_No template found. See docs/domain/_template-knowledge.md for format._\n"
        )

    return knowledge_path, True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_leads(project_name: str, *, dry_run: bool = False) -> list[str]:
    """Generate role docs + Themis YAML + knowledge seeds for all leads.

    Parameters
    ----------
    project_name:
        Name of the project whose central manifest to load.
        Reads ``${XDG_DATA_HOME:-~/.local/share}/khimaira/leads/<project_name>.toml``.
    dry_run:
        If True, return the list of files that WOULD be written without
        actually writing anything (used by ``--check``).

    Returns
    -------
    list[str]
        Summary lines describing what was generated.
    """
    manifest = load_manifest(project_name)
    summary: list[str] = []

    for domain, lead_cfg in manifest.leads.items():
        lead_name = _lead_name(domain, manifest.prefix)

        # --- Role doc ---
        role_path = manifest.roles_dir / f"{lead_name}.md"
        existing_role = role_path.read_text() if role_path.exists() else None
        rendered_role = _render_role_doc(manifest, domain, lead_cfg, existing_role)
        if not dry_run:
            role_path.parent.mkdir(parents=True, exist_ok=True)
            role_path.write_text(rendered_role)
        summary.append(f"  role doc   → {role_path}")

        # --- Themis YAML ---
        themis_path = manifest.themis_dir / f"{lead_name}.yaml"
        rendered_themis = _render_themis_yaml(manifest, domain, lead_cfg)
        if not dry_run:
            themis_path.parent.mkdir(parents=True, exist_ok=True)
            themis_path.write_text(rendered_themis)
        summary.append(f"  themis     → {themis_path}")

        # --- Knowledge seed (never clobbers existing) ---
        knowledge_path = manifest.knowledge_dir / f"{domain}-knowledge.md"
        if knowledge_path.exists():
            summary.append(f"  knowledge  → {knowledge_path} (exists, skipped)")
        else:
            if not dry_run:
                _seed_knowledge_doc(manifest, domain)
            summary.append(f"  knowledge  → {knowledge_path} (seeded)")

    return summary


def check_drift(project_name: str) -> tuple[bool, list[str]]:
    """Regenerate in-memory and diff against on-disk files.

    Returns
    -------
    (has_drift, diff_lines)
        ``has_drift`` is True if any generated file differs from on-disk.

    Note: knowledge docs are seeded-only (never clobbered by sync), so they
    are intentionally excluded from drift detection. Only role docs and Themis
    YAML are checked.
    """
    manifest = load_manifest(project_name)
    has_drift = False
    diff_lines: list[str] = []

    for domain, lead_cfg in manifest.leads.items():
        lead_name = _lead_name(domain, manifest.prefix)

        # Role doc
        role_path = manifest.roles_dir / f"{lead_name}.md"
        existing_role = role_path.read_text() if role_path.exists() else None
        rendered_role = _render_role_doc(manifest, domain, lead_cfg, existing_role)
        if not role_path.exists():
            has_drift = True
            diff_lines.append(f"MISSING: {role_path}")
        elif rendered_role != existing_role:
            has_drift = True
            diff = list(
                difflib.unified_diff(
                    (existing_role or "").splitlines(keepends=True),
                    rendered_role.splitlines(keepends=True),
                    fromfile=f"{role_path} (on-disk)",
                    tofile=f"{role_path} (generated)",
                )
            )
            diff_lines.extend(diff)

        # Themis YAML
        themis_path = manifest.themis_dir / f"{lead_name}.yaml"
        rendered_themis = _render_themis_yaml(manifest, domain, lead_cfg)
        existing_themis = themis_path.read_text() if themis_path.exists() else None
        if not themis_path.exists():
            has_drift = True
            diff_lines.append(f"MISSING: {themis_path}")
        elif rendered_themis != existing_themis:
            has_drift = True
            diff = list(
                difflib.unified_diff(
                    (existing_themis or "").splitlines(keepends=True),
                    rendered_themis.splitlines(keepends=True),
                    fromfile=f"{themis_path} (on-disk)",
                    tofile=f"{themis_path} (generated)",
                )
            )
            diff_lines.extend(diff)

    return has_drift, diff_lines
