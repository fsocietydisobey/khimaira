"""Semantic page structure analysis.

Builds a structural map of the current page by walking the DOM for
ARIA landmarks, roles, and stateful elements. Returns a concise tree
that tells Claude: what sections exist, which tab is selected, what's
expanded/collapsed, where the main content is, etc.

This is the browser's semantic understanding of the page — the same
model screen readers use — extracted as a JSON tree Claude can reason
about.
"""

from __future__ import annotations

import json
from typing import Any

from specter.browser.connection import CDPConnection

PAGE_STRUCTURE_SCRIPT = """
(() => {
    const LANDMARK_ROLES = new Set([
        'banner', 'navigation', 'main', 'complementary', 'contentinfo',
        'form', 'search', 'region',
    ]);

    const WIDGET_ROLES = new Set([
        'dialog', 'alertdialog', 'tablist', 'tab', 'tabpanel',
        'tree', 'treeitem', 'menu', 'menubar', 'menuitem',
        'toolbar', 'tooltip', 'alert',
    ]);

    const SECTION_ROLES = new Set([
        'group', 'list', 'listitem', 'heading',
        'article', 'section',
    ]);

    const ALL_INTERESTING = new Set([...LANDMARK_ROLES, ...WIDGET_ROLES, ...SECTION_ROLES]);

    // Map HTML elements to implicit ARIA roles
    function getRole(el) {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit;

        const tag = el.tagName.toLowerCase();
        const roleMap = {
            'nav': 'navigation',
            'main': 'main',
            'header': 'banner',
            'footer': 'contentinfo',
            'aside': 'complementary',
            'form': 'form',
            'dialog': 'dialog',
            'ul': 'list',
            'ol': 'list',
            'li': 'listitem',
            'h1': 'heading', 'h2': 'heading', 'h3': 'heading',
            'h4': 'heading', 'h5': 'heading', 'h6': 'heading',
            'table': 'table',
            'section': 'region',
            'article': 'article',
        };
        return roleMap[tag] || null;
    }

    function getLabel(el) {
        return el.getAttribute('aria-label')
            || el.getAttribute('aria-labelledby') && document.getElementById(el.getAttribute('aria-labelledby'))?.textContent?.trim()
            || el.getAttribute('title')
            || (el.tagName.match(/^H[1-6]$/) ? el.textContent?.trim().substring(0, 80) : null)
            || null;
    }

    function getState(el) {
        const state = {};
        if (el.getAttribute('aria-expanded') !== null)
            state.expanded = el.getAttribute('aria-expanded') === 'true';
        if (el.getAttribute('aria-selected') !== null)
            state.selected = el.getAttribute('aria-selected') === 'true';
        if (el.getAttribute('aria-checked') !== null)
            state.checked = el.getAttribute('aria-checked') === 'true';
        if (el.getAttribute('aria-hidden') !== null)
            state.hidden = el.getAttribute('aria-hidden') === 'true';
        if (el.getAttribute('aria-disabled') !== null)
            state.disabled = el.getAttribute('aria-disabled') === 'true';
        if (el.hasAttribute('open'))
            state.open = true;
        if (el.getAttribute('aria-current'))
            state.current = el.getAttribute('aria-current');
        return Object.keys(state).length > 0 ? state : null;
    }

    // Count interactive children (for sizing sections)
    function countInteractives(el) {
        return el.querySelectorAll('a, button, input, select, textarea, [role="button"], [role="link"]').length;
    }

    // Get visible text summary (first 60 chars of direct text, not children)
    function getTextSummary(el) {
        // For headings, return full text
        if (el.tagName.match(/^H[1-6]$/)) return el.textContent?.trim().substring(0, 100);

        // For other elements, get only direct text nodes
        let text = '';
        for (const node of el.childNodes) {
            if (node.nodeType === 3) { // TEXT_NODE
                text += node.textContent;
            }
        }
        text = text.trim();
        return text ? text.substring(0, 60) : null;
    }

    function buildTree(el, depth, maxDepth) {
        if (depth > maxDepth) return null;

        const role = getRole(el);
        const isInteresting = role && ALL_INTERESTING.has(role);

        // Also consider elements with data-testid or meaningful classes as interesting
        const hasTestId = el.dataset?.testid;
        const hasAriaLabel = el.getAttribute('aria-label');

        if (!isInteresting && !hasTestId && depth > 2) {
            // Not interesting itself — but check children
            const childResults = [];
            for (const child of el.children) {
                if (child.offsetParent === null && child.tagName !== 'DIALOG') continue;
                const result = buildTree(child, depth, maxDepth);
                if (result) {
                    if (Array.isArray(result)) childResults.push(...result);
                    else childResults.push(result);
                }
            }
            return childResults.length > 0 ? childResults : null;
        }

        if (!isInteresting && !hasTestId && !hasAriaLabel) {
            // Walk children looking for interesting descendants
            const childResults = [];
            for (const child of el.children) {
                const result = buildTree(child, depth + 1, maxDepth);
                if (result) {
                    if (Array.isArray(result)) childResults.push(...result);
                    else childResults.push(result);
                }
            }
            return childResults.length > 0 ? childResults : null;
        }

        const node = {
            role: role || el.tagName.toLowerCase(),
        };

        const label = getLabel(el) || hasTestId;
        if (label) node.label = label;

        const state = getState(el);
        if (state) node.state = state;

        const text = getTextSummary(el);
        if (text && !label) node.text = text;

        // Only add interactiveCount for container elements
        if (LANDMARK_ROLES.has(role) || role === 'tabpanel' || role === 'region' || role === 'dialog') {
            node.interactives = countInteractives(el);
        }

        // Recurse into children
        const children = [];
        for (const child of el.children) {
            if (child.offsetParent === null && child.tagName !== 'DIALOG') continue;
            const result = buildTree(child, depth + 1, maxDepth);
            if (result) {
                if (Array.isArray(result)) children.push(...result);
                else children.push(result);
            }
        }

        if (children.length > 0) {
            // Cap children to avoid huge output
            node.children = children.slice(0, 30);
            if (children.length > 30) node.childrenTruncated = children.length;
        }

        return node;
    }

    const tree = buildTree(document.body, 0, 8);
    return JSON.stringify(tree || { note: 'No semantic structure detected' });
})()
"""


class StructureAnalyzer:
    """Semantic page structure analysis."""

    async def get_page_structure(self, connection: CDPConnection) -> dict | list:
        """Build a semantic structure map of the current page.

        Walks the DOM looking for ARIA landmarks (navigation, main,
        complementary), widget roles (tablist, dialog, menu), and section
        roles (region, heading, group). Returns a tree showing:

          - What major sections exist (nav, main, sidebar, dialog)
          - Which tab is selected, what's expanded/collapsed
          - How many interactive elements each section contains
          - Labels and text summaries for orientation

        This is the "what am I looking at?" tool — it gives Claude a
        mental model of the page layout without needing to parse the
        screenshot visually.

        Returns:
            Nested tree of page sections with roles, labels, and states.
        """
        result = await connection.send(
            "Runtime.evaluate",
            {"expression": PAGE_STRUCTURE_SCRIPT, "returnByValue": True},
        )

        value = result.get("result", {}).get("value")
        if value is None:
            exc = result.get("exceptionDetails", {})
            return {"error": exc.get("text", "Failed to analyze page structure")}

        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
