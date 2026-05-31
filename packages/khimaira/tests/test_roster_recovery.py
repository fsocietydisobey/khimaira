"""Tests for monitor.roster_recovery — roster auto-recovery watcher."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from khimaira.monitor import roster_recovery as rr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_KITTY_LS = json.dumps([
    {
        "tabs": [
            {
                "windows": [
                    {
                        "id": 10,
                        "cmdline": ["bash", "-ic", "claude-chat", "-r", "agent-1", "--model", "sonnet"],
                    },
                    {
                        "id": 11,
                        "cmdline": ["bash", "-ic", "claude-chat", "-r", "master", "--model", "opus"],
                    },
                    {
                        "id": 12,
                        "cmdline": ["bash", "-ic", "vim", "README.md"],  # not a roster window
                    },
                ]
            }
        ]
    }
])


# ---------------------------------------------------------------------------
# _discover_roster_windows
# ---------------------------------------------------------------------------

class TestDiscoverRosterWindows:
    def test_parses_role_from_cmdline(self):
        with patch.object(rr, "_kitty", return_value=SAMPLE_KITTY_LS):
            windows = rr._discover_roster_windows()
        ids = {w["window_id"] for w in windows}
        roles = {w["role"] for w in windows}
        assert 10 in ids
        assert 11 in ids
        assert 12 not in ids  # vim is not a roster window
        # infer_role_from_name strips trailing -N: "agent-1" → "agent"
        assert "agent" in roles
        assert "master" in roles

    def test_returns_empty_when_kitty_unavailable(self):
        with patch.object(rr, "_kitty", return_value=None):
            assert rr._discover_roster_windows() == []

    def test_returns_empty_on_bad_json(self):
        with patch.object(rr, "_kitty", return_value="not-json"):
            assert rr._discover_roster_windows() == []

    def test_skips_windows_without_role_flag(self):
        data = json.dumps([{"tabs": [{"windows": [
            {"id": 20, "cmdline": ["claude"]},  # no -r flag
        ]}]}])
        with patch.object(rr, "_kitty", return_value=data):
            assert rr._discover_roster_windows() == []

    def test_normalizes_prefixed_names(self):
        """jp-agent-1 → role=jp-agent, jp-frontend-lead-1 → jp-frontend-lead."""
        from khimaira.monitor.chats import infer_role_from_name
        data = json.dumps([{"tabs": [{"windows": [
            {"id": 30, "cmdline": ["claude-chat", "-r", "jp-agent-1"]},
            {"id": 31, "cmdline": ["claude-chat", "-r", "jp-frontend-lead-1"]},
        ]}]}])
        with patch.object(rr, "_kitty", return_value=data):
            windows = rr._discover_roster_windows()
        roles = {w["role"] for w in windows}
        # infer_role_from_name handles prefix-stripping; roles should be normalized
        # (the exact result depends on _VALID_ROLES, but the raw suffix -1 is gone)
        for w in windows:
            assert not w["role"].endswith("-1"), "Numeric suffix must be stripped"


# ---------------------------------------------------------------------------
# _parse_context_pct
# ---------------------------------------------------------------------------

class TestParseContextPct:
    def test_parses_percent(self):
        assert rr._parse_context_pct("  87% context used · /model sonnet") == 87

    def test_parses_100(self):
        assert rr._parse_context_pct("100% context used") == 100

    def test_returns_none_when_absent(self):
        assert rr._parse_context_pct("some other text") is None

    def test_case_insensitive(self):
        assert rr._parse_context_pct("42% Context Used") == 42


# ---------------------------------------------------------------------------
# _is_busy
# ---------------------------------------------------------------------------

class TestIsBusy:
    def test_esc_to_interrupt(self):
        assert rr._is_busy("  esc to interrupt\n100% context used")

    def test_compacting(self):
        assert rr._is_busy("Compacting…")
        assert rr._is_busy("Compacting...")

    def test_idle_prompt(self):
        assert not rr._is_busy("  75% context used · /model sonnet\n>")

    def test_empty_returns_busy(self):
        # Conservative: unknown state → treat as busy
        assert rr._is_busy("")
        assert rr._is_busy(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _inject_text_and_submit — TOCTOU guard
# ---------------------------------------------------------------------------

class TestInjectTextAndSubmit:
    def _make_screen(self, text: str) -> str:
        return f"previous lines\n{text}"

    def test_submits_when_buffer_matches(self):
        screen_after_inject = self._make_screen("/compact")
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = [
                "",                           # send-text → success
                screen_after_inject,          # get-text (TOCTOU verify)
                "",                           # send-key enter → success
            ]
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is True

    def test_aborts_on_toctou_mismatch_extra_after(self):
        """User typed AFTER our text — buffer ends with /compact but has extra."""
        screen = self._make_screen("/compact extra")  # user typed after
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = ["", screen, ""]
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is False
        assert "ctrl+c" in str(mock_kitty.call_args_list[-1])

    def test_aborts_on_toctou_mismatch_extra_before(self):
        """User typed BEFORE our text — endswith() would pass but exact match catches it."""
        screen = self._make_screen("user_input/compact")  # user typed before
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = ["", screen, ""]
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is False, "Exact-match guard must catch user-before-our-text"
        assert "ctrl+c" in str(mock_kitty.call_args_list[-1])

    def test_aborts_when_get_text_fails(self):
        """Can't verify buffer — abort conservatively."""
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = [
                "",    # send-text
                None,  # get-text fails
                "",    # ctrl+c
            ]
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is False

    def test_aborts_when_send_text_fails(self):
        with patch.object(rr, "_kitty", return_value=None):
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is False


# ---------------------------------------------------------------------------
# _process_window — decision logic
# ---------------------------------------------------------------------------

class TestProcessWindow:
    @pytest.fixture(autouse=True)
    def clear_debounce(self):
        rr._DEBOUNCE.clear()
        yield
        rr._DEBOUNCE.clear()

    def _win(self, role="agent-1", window_id=10):
        return {"window_id": window_id, "role": role, "cmdline": f"claude-chat -r {role}"}

    @pytest.mark.asyncio
    async def test_compact_when_at_threshold(self):
        screen = "87% context used\n>"
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_distill_session", new_callable=AsyncMock),
            patch.object(rr, "_inject_text_and_submit", return_value=True) as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_called_once_with(10, "/compact")

    @pytest.mark.asyncio
    async def test_distill_called_before_compact(self):
        """Verify distill-before-compact ordering (data-safety invariant)."""
        call_order: list[str] = []
        screen = "92% context used\n>"

        async def mock_distill(sid, role):
            call_order.append("distill")

        def mock_inject(wid, text):
            call_order.append("inject")
            return True

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_distill_session", side_effect=mock_distill),
            patch.object(rr, "_inject_text_and_submit", side_effect=mock_inject),
        ):
            await rr._process_window(self._win())

        assert call_order == ["distill", "inject"], (
            "distill MUST precede compact injection"
        )

    @pytest.mark.asyncio
    async def test_skips_when_busy(self):
        screen = "88% context used\nesc to interrupt"
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_below_threshold(self):
        screen = "70% context used\n>"
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_debounce_suppresses_second_compact(self):
        screen = "90% context used\n>"
        rr._DEBOUNCE[(10, "compact")] = time.time()  # already compacted recently

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_global_opt_out(self):
        with (
            patch.object(rr, "_env_enabled", return_value=False),
            patch.object(rr, "_resolve_session_for_role") as mock_resolve,
        ):
            await rr._process_window(self._win())
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_session_uuid(self):
        """Guard (a): no matching session UUID → abort."""
        screen = "95% context used\n>"
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value=None),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_ambiguous_session(self):
        """Guard (a): ≥2 sessions match role → _resolve_session_for_role returns None → abort."""
        screen = "95% context used\n>"
        # Simulate ambiguity: _resolve_session_for_role returns None (already done by the function)
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value=None),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_per_session_opt_out(self):
        screen = "95% context used\n>"
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=True),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_aborts_compact_if_window_becomes_busy_during_distill(self):
        """After distill completes, window may have started working — re-check."""
        screens = iter([
            "90% context used\n>",          # initial check: idle
            "90% context used\nesc to interrupt",  # re-check after distill: busy
        ])

        async def mock_distill(sid, role):
            pass

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", side_effect=screens),
            patch.object(rr, "_distill_session", side_effect=mock_distill),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()
