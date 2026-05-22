"""Accessibility auditing via axe-core injection.

Provides a single entry point: A11yAuditor.audit(conn, selector=None).

axe-core strategy:
  Vendored at specter/_vendored/axe.min.js (v4.10.2, committed to the repo).
  Rationale: vendoring ensures CI reproducibility without CDN access; avoids
  the same CDN-flakiness risk that prompted concern in SLICE-T's fixture.html.
  Update: run `curl -fsSL <cdnjs-axe-url> -o src/specter/_vendored/axe.min.js`
  and bump the version comment in this file.

Injection is idempotent: the script checks `window.__axe_loaded` before
injecting to avoid double-registering the library if the tool is called twice.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from specter.browser.connection import CDPConnection

logger = logging.getLogger(__name__)

_AXE_JS_PATH = Path(__file__).parent.parent / "_vendored" / "axe.min.js"


def _load_axe_js() -> str:
    """Load vendored axe-core JS; raise FileNotFoundError if missing."""
    if not _AXE_JS_PATH.exists():
        raise FileNotFoundError(
            f"axe-core not found at {_AXE_JS_PATH}. "
            "Run: curl -fsSL https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js "
            f"-o {_AXE_JS_PATH}"
        )
    return _AXE_JS_PATH.read_text(encoding="utf-8")


class A11yAuditor:
    """Runs axe-core accessibility audits against the active page."""

    async def audit(
        self,
        conn: "CDPConnection",
        selector: str | None = None,
    ) -> dict:
        """Inject axe-core and run an accessibility audit.

        Audits the full page (default) or a selector-scoped subtree.
        Returns WCAG 2.1 AA violations, passes, inapplicable rules, and
        incomplete checks.

        Large pages may take 5-15s; Specter's default CDP timeout handles this.

        WCAG 2.1 AA by default. Pass a selector to scope the audit (faster and
        avoids noise from page regions outside your area of interest).

        Args:
            conn: Active CDP connection.
            selector: Optional CSS selector to scope the audit. If None, audits
                the full document.

        Returns:
            Dict with:
                violations: list of {id, impact, description, nodes, helpUrl}
                passes: list of {id, description}
                inapplicable: list of rule IDs not applicable to this page
                incomplete: list of rules axe could not complete (need manual check)
                axe_version: axe-core version string
            On error: {"error": "<reason>"}
        """
        try:
            axe_js = _load_axe_js()
        except FileNotFoundError as exc:
            return {"error": str(exc)}

        # Inject axe-core (idempotent: skip if already loaded)
        inject_script = f"""
(function() {{
    if (window.__axe_loaded) return 'already_loaded';
    {axe_js};
    window.__axe_loaded = true;
    return 'injected';
}})()
"""
        inject_result = await conn.send(
            "Runtime.evaluate",
            {"expression": inject_script, "returnByValue": True},
        )
        inject_status = inject_result.get("result", {}).get("value", "")
        if "error" in inject_result.get("exceptionDetails", {}):
            exc_text = inject_result["exceptionDetails"].get("text", "injection failed")
            logger.error("axe-core injection failed: %s", exc_text)
            return {"error": f"axe-core injection failed: {exc_text}"}
        logger.debug("axe-core inject status: %s", inject_status)

        # Build axe.run() call — scope to selector if provided
        if selector:
            safe_sel = selector.replace("'", "\\'")
            run_expr = f"axe.run(document.querySelector('{safe_sel}'))"
        else:
            run_expr = "axe.run()"

        audit_result = await conn.send(
            "Runtime.evaluate",
            {
                "expression": run_expr,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )

        if "exceptionDetails" in audit_result:
            exc_text = audit_result["exceptionDetails"].get("text", "axe.run() failed")
            return {"error": f"axe.run() failed: {exc_text}"}

        raw = audit_result.get("result", {}).get("value")
        if raw is None:
            return {"error": "axe.run() returned null — is the page fully loaded?"}

        return {
            "violations": [
                {
                    "id": v.get("id"),
                    "impact": v.get("impact"),
                    "description": v.get("description"),
                    "helpUrl": v.get("helpUrl"),
                    "nodes": [
                        {
                            "target": n.get("target"),
                            "html": n.get("html"),
                            "failureSummary": n.get("failureSummary"),
                        }
                        for n in v.get("nodes", [])
                    ],
                }
                for v in raw.get("violations", [])
            ],
            "passes": [
                {"id": p.get("id"), "description": p.get("description")}
                for p in raw.get("passes", [])
            ],
            "inapplicable": [r.get("id") for r in raw.get("inapplicable", [])],
            "incomplete": [
                {"id": r.get("id"), "description": r.get("description")}
                for r in raw.get("incomplete", [])
            ],
            "axe_version": raw.get("testEngine", {}).get("version", "unknown"),
        }
