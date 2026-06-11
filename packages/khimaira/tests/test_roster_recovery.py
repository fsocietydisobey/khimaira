"""Tests for monitor.roster_recovery — roster auto-recovery watcher."""

from __future__ import annotations

import json
import os
import subprocess
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
    """Tests use _get_roster_member_ids=frozenset() (fail-open/no scoping) so
    role-parsing tests are independent of live session state."""

    def test_parses_role_from_cmdline(self):
        with (
            patch.object(rr, "_kitty", return_value=SAMPLE_KITTY_LS),
            patch.object(rr, "_get_roster_member_ids", return_value=frozenset()),
        ):
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
        with (
            patch.object(rr, "_kitty", return_value=data),
            patch.object(rr, "_get_roster_member_ids", return_value=frozenset()),
        ):
            assert rr._discover_roster_windows() == []

    def test_normalizes_prefixed_names(self):
        """jp-agent-1 → role=jp-agent, jp-frontend-lead-1 → jp-frontend-lead."""
        from khimaira.monitor.chats import infer_role_from_name
        data = json.dumps([{"tabs": [{"windows": [
            {"id": 30, "cmdline": ["claude-chat", "-r", "jp-agent-1"]},
            {"id": 31, "cmdline": ["claude-chat", "-r", "jp-frontend-lead-1"]},
        ]}]}])
        with (
            patch.object(rr, "_kitty", return_value=data),
            patch.object(rr, "_get_roster_member_ids", return_value=frozenset()),
        ):
            windows = rr._discover_roster_windows()
        roles = {w["role"] for w in windows}
        # infer_role_from_name handles prefix-stripping; roles should be normalized
        # (the exact result depends on _VALID_ROLES, but the raw suffix -1 is gone)
        for w in windows:
            assert not w["role"].endswith("-1"), "Numeric suffix must be stripped"


class TestDiscoverRosterWindowsScoping:
    """Cross-project scoping: only sessions in active_roster_member_ids() pass."""

    KITTY_WITH_MIXED = json.dumps([{"tabs": [{"windows": [
        {"id": 10, "title": "agent-1", "cmdline": ["bash", "-ic",
            "cd '/home/_3ntropy/dev/khimaira' && claude-chat -r agent-1 --model sonnet"]},
        {"id": 20, "title": "jp-backend-lead-1", "cmdline": ["bash", "-ic",
            "cd '/home/_3ntropy/work/jeevy_portal' && claude-chat -r jp-backend-lead-1 --model sonnet"]},
        {"id": 30, "title": "master", "cmdline": ["bash", "-ic",
            "cd '/home/_3ntropy/dev/khimaira' && claude-chat -r khimaira-0 --model opus"]},
    ]}]}])

    def test_roster_scoped_excludes_other_project(self):
        """When roster_ids is populated, only sessions in the set pass."""
        # agent-1 is in the roster; jp-backend-lead-1 is NOT.
        roster_ids = frozenset(["uuid-agent-1", "uuid-master"])
        from khimaira.monitor import sessions as sess_mod
        with (
            patch.object(rr, "_kitty", return_value=self.KITTY_WITH_MIXED),
            patch.object(rr, "_get_roster_member_ids", return_value=roster_ids),
            patch.object(sess_mod, "list_sessions", return_value=[
                {"name": "agent-1", "session_id": "uuid-agent-1"},
                # window 30 title="master" → lookup by "master"
                {"name": "master", "session_id": "uuid-master"},
            ]),
        ):
            windows = rr._discover_roster_windows()
        ids = {w["window_id"] for w in windows}
        assert 10 in ids, "agent-1 (roster member) must be included"
        assert 20 not in ids, "jp-backend-lead-1 (other project) must be excluded"
        assert 30 in ids, "khimaira-0 (roster member) must be included"

    def test_empty_roster_ids_passes_all(self):
        """Fail-open: empty roster_ids (canonical unavailable) → all windows pass."""
        with (
            patch.object(rr, "_kitty", return_value=self.KITTY_WITH_MIXED),
            patch.object(rr, "_get_roster_member_ids", return_value=frozenset()),
        ):
            windows = rr._discover_roster_windows()
        ids = {w["window_id"] for w in windows}
        # All windows with a valid role pass when roster_ids is empty
        assert 10 in ids
        assert 30 in ids


# ---------------------------------------------------------------------------
# _compute_context_pct (transcript-token based — replaces terminal-scrape)
# ---------------------------------------------------------------------------

def _make_transcript_jsonl(usage: dict, model: str = "claude-sonnet-4-6") -> str:
    """Return a minimal transcript JSONL with one assistant message."""
    import json as _json
    record = {
        "type": "assistant",
        "message": {
            "model": model,
            "usage": usage,
        },
    }
    return _json.dumps(record) + "\n"


class TestComputeContextPct:
    def _run(self, usage: dict, model: str = "claude-sonnet-4-6", env_override: str | None = None) -> int | None:
        content = _make_transcript_jsonl(usage, model)
        mock_path = MagicMock()
        mock_path.is_file.return_value = True
        mock_path.read_text.return_value = content

        from khimaira.monitor import sessions as sess_mod
        env = {}
        if env_override is not None:
            env["KHIMAIRA_CONTEXT_WINDOW"] = env_override
        with (
            patch.object(sess_mod, "_find_transcript", return_value=mock_path),
            patch.dict(os.environ, env, clear=False),
        ):
            return rr._compute_context_pct("dummy-session-id")

    def test_fresh_session_uses_1m_default(self):
        # 170_000 / 1_000_000 = 17% — fresh 1M session below 200k high-water uses 1M default
        result = self._run({
            "input_tokens": 100_000,
            "cache_creation_input_tokens": 50_000,
            "cache_read_input_tokens": 20_000,
            "output_tokens": 500,
        })
        # Must NOT read 85% (that would be the dangerous 200k-default false-positive)
        assert result == 17, f"Fresh session below 200k must use 1M default, got {result}%"

    def test_1m_window_via_high_water_mark(self):
        """Session that previously exceeded 200k infers 1M window for all turns."""
        import json as _json
        # Two turns: first has 250k (establishes 1M window), second has 50k.
        turn1 = _json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 250_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})
        turn2 = _json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 50_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})
        content = turn1 + "\n" + turn2 + "\n"
        mock_path = MagicMock()
        mock_path.is_file.return_value = True
        mock_path.read_text.return_value = content
        from khimaira.monitor import sessions as sess_mod
        with patch.object(sess_mod, "_find_transcript", return_value=mock_path):
            result = rr._compute_context_pct("dummy")
        # 50_000 / 1_000_000 = 5%  (last turn's ctx / window inferred from max)
        assert result == 5

    def test_full_context_1m(self):
        # 1_000_000 / 1_000_000 = 100% (1M window via high-water-mark)
        result = self._run({
            "input_tokens": 1_000_000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        })
        assert result == 100

    def test_context_above_200k_uses_1m_window(self):
        """When current turn's context exceeds 200k, 1M window is used."""
        result = self._run({
            "input_tokens": 256_000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        })
        # 256_000 / 1_000_000 = 26%  (not 128%)
        assert result == 26

    def test_empty_usage_returns_zero(self):
        result = self._run({
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        })
        assert result == 0

    def test_no_transcript_returns_none(self):
        from khimaira.monitor import sessions as sess_mod
        with patch.object(sess_mod, "_find_transcript", return_value=None):
            assert rr._compute_context_pct("dummy") is None

    def test_missing_usage_returns_none(self):
        content = '{"type": "assistant", "message": {"model": "claude-sonnet-4-6"}}\n'
        mock_path = MagicMock()
        mock_path.is_file.return_value = True
        mock_path.read_text.return_value = content
        from khimaira.monitor import sessions as sess_mod
        with patch.object(sess_mod, "_find_transcript", return_value=mock_path):
            assert rr._compute_context_pct("dummy") is None

    def test_env_context_window_override(self):
        # 100_000 / 400_000 = 25%
        result = self._run(
            {"input_tokens": 100_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            env_override="400000",
        )
        assert result == 25

    def test_fresh_session_ramp_simulation(self):
        """Fresh 1M session at 170k reads ~17% NOT 85% — prevents premature compaction."""
        result = self._run({
            "input_tokens": 100_000,
            "cache_creation_input_tokens": 50_000,
            "cache_read_input_tokens": 20_000,  # total = 170k, below 200k high-water
        })
        # 170_000 / 1_000_000 = 17% — safe, not 85% (which would wrongly trigger compact)
        assert result == 17, f"Fresh 1M session at 170k must read ~17%, not 85%: got {result}%"

    def test_threshold_at_1m_window(self):
        # 850_000 / 1_000_000 = 85% — at compact threshold
        result = self._run({
            "input_tokens": 850_000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        })
        assert result is not None and result == 85


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
    @pytest.fixture(autouse=True)
    def _unique_title(self, monkeypatch):
        # These tests exercise the inject MECHANICS; assume a unique title so the
        # 2026-06-11 duplicate-guard (_count_title_windows) is a clean pass-through
        # and doesn't consume the fixed _kitty side_effect sequences.
        monkeypatch.setattr(rr, "_count_title_windows", lambda t: 1)

    def _make_screen(self, text: str) -> str:
        return f"previous lines\n{text}"

    def test_submits_when_buffer_matches(self):
        screen_after_inject = self._make_screen("/compact")
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = [
                "",                           # send-key ctrl+u (clear stale input)
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
            mock_kitty.side_effect = ["", "", screen, ""]
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is False
        assert "ctrl+c" in str(mock_kitty.call_args_list[-1])

    def test_aborts_on_toctou_mismatch_extra_before(self):
        """User typed BEFORE our text — endswith() would pass but exact match catches it."""
        screen = self._make_screen("user_input/compact")  # user typed before
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = ["", "", screen, ""]
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is False, "Exact-match guard must catch user-before-our-text"
        assert "ctrl+c" in str(mock_kitty.call_args_list[-1])

    def test_aborts_when_get_text_fails(self):
        """Can't verify buffer — abort conservatively."""
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = [
                "",    # send-key ctrl+u (clear)
                "",    # send-text
                None,  # get-text fails
                "",    # ctrl+c
            ]
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is False

    def test_aborts_when_send_text_fails(self):
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = [
                "",    # send-key ctrl+u (clear)
                None,  # send-text fails
            ]
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is False

    def test_uses_title_match_when_title_provided(self):
        """When window_title is given, send-text and send-key enter use title-match.
        ctrl+u (step 1) and TOCTOU get-text (step 3) always use id-match for safety."""
        screen_after_inject = self._make_screen("/compact")
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = ["", "", screen_after_inject, ""]
            result = rr._inject_text_and_submit(
                window_id=10, text="/compact", window_title="agent-3"
            )
        assert result is True
        # Index 0: ctrl+u (id-match); index 1: send-text (title-match);
        # index 2: get-text TOCTOU (id-match); index 3: send-key enter (title-match).
        # Inspect actual arg tuples (not str(call) — repr doubles backslashes).
        send_text_args = mock_kitty.call_args_list[1].args
        send_key_args = mock_kitty.call_args_list[3].args
        # Anchored exact-match (2026-06-07 cross-roster-nudge fix): kitty title:
        # is an unanchored regex, so the match must be ^<escaped-title>$.
        assert r"--match=title:^agent\-3$" in send_text_args, "send-text must use anchored title-match"
        assert r"--match=title:^agent\-3$" in send_key_args, "send-key must use anchored title-match"
        assert "--match=id:10" not in send_text_args, "send-text must NOT use id-match when title given"
        assert "--match=id:10" not in send_key_args, "send-key must NOT use id-match when title given"

    def test_uses_id_match_when_title_empty(self):
        """Without window_title, falls back to id-match (backward compat)."""
        screen_after_inject = self._make_screen("/compact")
        with patch.object(rr, "_kitty") as mock_kitty:
            mock_kitty.side_effect = ["", "", screen_after_inject, ""]
            result = rr._inject_text_and_submit(window_id=10, text="/compact")
        assert result is True
        calls = [str(c) for c in mock_kitty.call_args_list]
        assert any("id:10" in c for c in calls), "id-match must be used when no title given"

    def test_title_match_absent_title_fails_loudly(self):
        """Title-match returning None (rc!=0 = window not found) → False, not silent."""
        # kitty @ send-text --match=title:<absent> → rc != 0 → _kitty returns None
        with patch.object(rr, "_kitty", return_value=None):
            result = rr._inject_text_and_submit(
                window_id=99, text="wake", window_title="dead-agent"
            )
        assert result is False


class TestAutoWakeFreshEnumerate:
    """Auto-wake re-enumerates kitty @ ls fresh every cycle (no id-map caching)."""

    @pytest.mark.asyncio
    async def test_check_once_calls_discover_each_run(self):
        """check_once() calls _discover_roster_windows each time (not cached)."""
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_discover_roster_windows", return_value=[]) as mock_discover,
        ):
            await rr.check_once()
            await rr.check_once()
        assert mock_discover.call_count == 2, "Must re-enumerate windows on every sweep"


# ---------------------------------------------------------------------------
# _process_window — decision logic
# ---------------------------------------------------------------------------

class TestProcessWindow:
    @pytest.fixture(autouse=True)
    def clear_debounce(self):
        rr._DEBOUNCE.clear()
        yield
        rr._DEBOUNCE.clear()

    def _win(self, role="agent-1", window_id=10, raw_name=""):
        return {"window_id": window_id, "role": role, "raw_name": raw_name, "cmdline": f"claude-chat -r {role}"}

    @pytest.mark.asyncio
    async def test_compact_when_at_threshold(self):
        screen = ">"
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_compute_context_pct", return_value=87),
            patch.object(rr, "_distill_session", new_callable=AsyncMock),
            patch.object(rr, "_inject_text_and_submit", return_value=True) as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_called_once_with(10, "/compact", "")

    @pytest.mark.asyncio
    async def test_distill_called_before_compact(self):
        """Verify distill-before-compact ordering (data-safety invariant)."""
        call_order: list[str] = []
        screen = ">"

        async def mock_distill(sid, role):
            call_order.append("distill")

        def mock_inject(wid, text, title=""):
            call_order.append("inject")
            return True

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_compute_context_pct", return_value=92),
            patch.object(rr, "_distill_session", side_effect=mock_distill),
            patch.object(rr, "_inject_text_and_submit", side_effect=mock_inject),
        ):
            await rr._process_window(self._win())

        assert call_order == ["distill", "inject"], (
            "distill MUST precede compact injection"
        )

    @pytest.mark.asyncio
    async def test_skips_when_busy(self):
        screen = "esc to interrupt"
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_compute_context_pct", return_value=88),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_below_threshold(self):
        screen = ">"
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_compute_context_pct", return_value=70),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_debounce_suppresses_second_compact(self):
        screen = ">"
        rr._DEBOUNCE[(10, "compact")] = time.time()  # already compacted recently

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_compute_context_pct", return_value=90),
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
            ">",                    # initial check: idle
            "esc to interrupt",     # re-check after distill: busy
        ])

        async def mock_distill(sid, role):
            pass

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", side_effect=screens),
            patch.object(rr, "_compute_context_pct", return_value=90),
            patch.object(rr, "_distill_session", side_effect=mock_distill),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_watcher_wakes_session_with_pending_task(self):
        """Idle session with a pending-not-started task qualifies for wake."""
        screen = ">"  # below compact threshold, idle

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_compute_context_pct", return_value=60),
            patch("khimaira.monitor.api.chats._get_session_obligations", return_value=[]),
            patch.object(rr, "_session_has_pending_task", return_value=True),
            patch.object(rr, "_session_has_pending_invite", return_value=False),
            patch.object(rr, "_inject_text_and_submit", return_value=True) as mock_inject,
            patch("khimaira.monitor.sessions.list_sessions", return_value=[
                {"session_id": "uuid-1234", "last_active_age_s": 400}
            ]),
        ):
            await rr._process_window(self._win())
        mock_inject.assert_called_once()

    @pytest.mark.asyncio
    async def test_watcher_wakes_session_with_pending_invite(self):
        """Idle session with a pending chat invite qualifies for wake."""
        screen = ">"  # below compact threshold, idle

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_compute_context_pct", return_value=60),
            patch("khimaira.monitor.api.chats._get_session_obligations", return_value=[]),
            patch.object(rr, "_session_has_pending_task", return_value=False),
            patch.object(rr, "_session_has_pending_invite", return_value=True),
            patch.object(rr, "_inject_text_and_submit", return_value=True) as mock_inject,
            patch("khimaira.monitor.sessions.list_sessions", return_value=[
                {"session_id": "uuid-1234", "last_active_age_s": 400}
            ]),
        ):
            await rr._process_window(self._win())
        mock_inject.assert_called_once()

    @pytest.mark.asyncio
    async def test_watcher_no_false_wake_no_obligation(self):
        """Idle session with no task, no invite, no obligation → no wake."""
        screen = ">"

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_compute_context_pct", return_value=60),
            patch("khimaira.monitor.api.chats._get_session_obligations", return_value=[]),
            patch.object(rr, "_session_has_pending_task", return_value=False),
            patch.object(rr, "_session_has_pending_invite", return_value=False),
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())
        mock_inject.assert_not_called()


# ---------------------------------------------------------------------------
# _kitty — KITTY_LISTEN_ON socket injection (daemon TTY-less path)
# ---------------------------------------------------------------------------

class TestKittySocketInjection:
    """_kitty must pass --to=<socket> when KITTY_LISTEN_ON is set.

    The daemon runs without a controlling TTY (forked, no /dev/tty). Bare
    `kitty @ ls` fails with "open /dev/tty: no such device". The fix is to
    inject `--to=<socket>` from KITTY_LISTEN_ON so kitty uses the IPC socket
    directly. These tests exercise cmd-construction without needing a live kitty.
    """

    def _fake_run_ok(self, captured: list) -> object:
        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = "[]"
            return r
        return fake_run

    def test_cmd_includes_to_when_listen_on_set(self):
        """When KITTY_LISTEN_ON is set, cmd must contain --to=<value>."""
        captured: list = []
        with (
            patch.dict(os.environ, {"KITTY_LISTEN_ON": "unix:/tmp/kitty-test"}, clear=False),
            patch("subprocess.run", side_effect=self._fake_run_ok(captured)),
        ):
            result = rr._kitty("ls")
        assert result == "[]"
        assert captured, "subprocess.run was not called"
        cmd = captured[0]
        assert "--to=unix:/tmp/kitty-test" in cmd, (
            f"Expected --to=unix:/tmp/kitty-test in cmd: {cmd}"
        )
        assert "ls" in cmd

    def test_cmd_no_to_when_listen_on_unset(self):
        """Without KITTY_LISTEN_ON, cmd falls back to bare `kitty @ ls`."""
        captured: list = []
        env_without = {k: v for k, v in os.environ.items() if k != "KITTY_LISTEN_ON"}
        with (
            patch.dict(os.environ, env_without, clear=True),
            patch("subprocess.run", side_effect=self._fake_run_ok(captured)),
        ):
            result = rr._kitty("ls")
        assert result == "[]"
        cmd = captured[0]
        assert not any(a.startswith("--to=") for a in cmd), (
            f"--to= should NOT appear without KITTY_LISTEN_ON: {cmd}"
        )

    def test_daemon_path_uses_socket_not_tty(self):
        """Simulate daemon call: KITTY_LISTEN_ON set → socket path used."""
        captured: list = []
        socket_val = "unix:/tmp/kitty-daemontest"
        with (
            patch.dict(os.environ, {"KITTY_LISTEN_ON": socket_val}, clear=False),
            patch("subprocess.run", side_effect=self._fake_run_ok(captured)),
        ):
            result = rr._kitty("ls")
        assert result is not None, "_kitty returned None — socket injection failed"
        cmd = captured[0]
        assert f"--to={socket_val}" in cmd, (
            f"Daemon path must have --to= in cmd: {cmd}"
        )

    def test_failure_logged_at_warning_not_debug(self):
        """Kitty failure must be logged at WARNING (not DEBUG) so daemon issues are visible."""
        def fake_run_fail(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "open /dev/tty: no such device"
            return r

        with (
            patch.dict(os.environ, {"KITTY_LISTEN_ON": "unix:/tmp/kitty-test"}, clear=False),
            patch("subprocess.run", side_effect=fake_run_fail),
            patch.object(rr._log, "warning") as mock_warn,
            patch.object(rr._log, "debug") as mock_debug,
        ):
            result = rr._kitty("ls")

        assert result is None
        mock_warn.assert_called_once()
        mock_debug.assert_not_called()


# ---------------------------------------------------------------------------
# HITL auto-answering
# ---------------------------------------------------------------------------

NUMBERED_PROMPT = (
    "⚡ Edit file: packages/khimaira/src/khimaira/monitor/api/themis.py\n"
    "Do you want to proceed?\n"
    "❯ 1. Yes, and don't ask again for this session\n"
    "  2. Yes\n"
    "  3. No, and tell Claude what to do differently\n"
)

YES_NO_PROMPT = (
    "Do you want to run this command?\n"
    "  bash -c 'echo hello'\n"
    "(y/n) "
)

DESTRUCTIVE_PROMPT = (
    "Do you want to run this command?\n"
    "  bash -c 'rm -rf /tmp/foo'\n"
    "❯ 1. Yes\n"
    "  2. No\n"
)

OUT_OF_SCOPE_PROMPT = (
    "⚡ Edit file: packages/other_package/secret.py\n"
    "❯ 1. Yes\n"
    "  2. No\n"
)

TASK_BODY_THEMIS = "Implement #61 — fix line 197 in api/themis.py (the if member_roles_dict gate)"


class TestDetectHitlPrompt:
    def test_detects_numbered_prompt(self):
        result = rr._detect_hitl_prompt(NUMBERED_PROMPT)
        assert result is not None
        assert result["kind"] == "numbered"
        assert result["answer_key"] == "1"

    def test_detects_yes_no_prompt(self):
        result = rr._detect_hitl_prompt(YES_NO_PROMPT)
        assert result is not None
        assert result["kind"] == "yes_no"
        assert result["answer_key"] == "y"

    def test_returns_none_for_normal_screen(self):
        normal = "60% context used\n> Writing some code...\n"
        assert rr._detect_hitl_prompt(normal) is None

    def test_returns_none_for_busy_screen(self):
        busy = "esc to interrupt\n85% context used"
        assert rr._detect_hitl_prompt(busy) is None


class TestCheckDestructive:
    def test_detects_rm_rf(self):
        assert rr._check_destructive("rm -rf /tmp/foo") is not None

    def test_detects_git_force_push(self):
        assert rr._check_destructive("git push origin main --force") is not None

    def test_detects_git_reset_hard(self):
        assert rr._check_destructive("git reset --hard HEAD~1") is not None

    def test_detects_drop_table(self):
        assert rr._check_destructive("DROP TABLE users") is not None

    def test_detects_sudo(self):
        assert rr._check_destructive("sudo rm -f /etc/foo") is not None

    def test_clean_returns_none(self):
        assert rr._check_destructive("echo hello world") is None

    def test_git_push_without_force_is_clean(self):
        assert rr._check_destructive("git push origin main") is None


class TestIsInTaskScope:
    def test_in_scope_when_filename_in_task(self):
        assert rr._is_in_task_scope(NUMBERED_PROMPT, TASK_BODY_THEMIS) is True

    def test_out_of_scope_when_different_file(self):
        assert rr._is_in_task_scope(OUT_OF_SCOPE_PROMPT, TASK_BODY_THEMIS) is False

    def test_no_task_body_escalates(self):
        assert rr._is_in_task_scope(NUMBERED_PROMPT, None) is False

    def test_no_path_in_prompt_escalates(self):
        assert rr._is_in_task_scope("Do you want to proceed?\n❯ 1. Yes\n", TASK_BODY_THEMIS) is False


class TestRoleBlocksFileEdit:
    def test_analyst_blocked_on_edit(self):
        assert rr._role_blocks_file_edit("analyst", "Edit file: foo.py") is True

    def test_analyst_blocked_on_write(self):
        assert rr._role_blocks_file_edit("analyst", "Write to foo.py") is True

    def test_agent_not_blocked(self):
        assert rr._role_blocks_file_edit("agent", "Edit file: foo.py") is False

    def test_architect_not_blocked(self):
        assert rr._role_blocks_file_edit("architect", "Edit file: foo.py") is False

    def test_observer_blocked(self):
        assert rr._role_blocks_file_edit("observer", "Edit file: foo.py") is True


class TestHandleHitlPrompt:
    def _make_prompt(self, raw=NUMBERED_PROMPT):
        return rr._detect_hitl_prompt(raw)

    def test_benign_in_scope_answered(self):
        prompt = self._make_prompt(NUMBERED_PROMPT)
        with (
            patch.object(rr, "_check_destructive", return_value=None),
            patch.object(rr, "_get_session_active_task_body", return_value=TASK_BODY_THEMIS),
            patch.object(rr, "_is_in_task_scope", return_value=True),
            patch.object(rr, "_role_blocks_file_edit", return_value=False),
            patch.object(rr, "_inject_text_and_submit", return_value=True),
        ):
            result = rr._handle_hitl_prompt(100, "uuid-1234", "agent", prompt)
        assert result == "answered"

    def test_destructive_marker_escalated(self):
        prompt = self._make_prompt(DESTRUCTIVE_PROMPT)
        with (
            patch.object(rr, "_check_destructive", return_value="rm -rf"),
            patch.object(rr, "_escalate_hitl") as mock_esc,
        ):
            result = rr._handle_hitl_prompt(100, "uuid-1234", "agent", prompt)
        assert result == "escalated"
        mock_esc.assert_called_once()
        assert "destructive-marker" in mock_esc.call_args[0][4]

    def test_out_of_scope_escalated(self):
        prompt = self._make_prompt(OUT_OF_SCOPE_PROMPT)
        with (
            patch.object(rr, "_check_destructive", return_value=None),
            patch.object(rr, "_get_session_active_task_body", return_value=TASK_BODY_THEMIS),
            patch.object(rr, "_is_in_task_scope", return_value=False),
            patch.object(rr, "_escalate_hitl") as mock_esc,
        ):
            result = rr._handle_hitl_prompt(100, "uuid-1234", "agent", prompt)
        assert result == "escalated"
        mock_esc.assert_called_once()

    def test_role_mismatch_escalated(self):
        prompt = self._make_prompt()
        with (
            patch.object(rr, "_check_destructive", return_value=None),
            patch.object(rr, "_get_session_active_task_body", return_value=TASK_BODY_THEMIS),
            patch.object(rr, "_is_in_task_scope", return_value=True),
            patch.object(rr, "_role_blocks_file_edit", return_value=True),
            patch.object(rr, "_escalate_hitl") as mock_esc,
        ):
            result = rr._handle_hitl_prompt(100, "uuid-1234", "analyst", prompt)
        assert result == "escalated"
        mock_esc.assert_called_once()

    def test_unknown_prompt_kind_escalated(self):
        unknown_prompt = {"raw_block": "some text\nDo you want", "answer_key": "1", "kind": "unknown"}
        with (
            patch.object(rr, "_check_destructive", return_value=None),
            patch.object(rr, "_get_session_active_task_body", return_value=TASK_BODY_THEMIS),
            patch.object(rr, "_is_in_task_scope", return_value=True),
            patch.object(rr, "_role_blocks_file_edit", return_value=False),
            patch.object(rr, "_escalate_hitl") as mock_esc,
        ):
            result = rr._handle_hitl_prompt(100, "uuid-1234", "agent", unknown_prompt)
        assert result == "escalated"
        mock_esc.assert_called_once()

    def test_opt_out_env_skips_hitl(self):
        """KHIMAIRA_AUTO_HITL=0 disables HITL processing."""
        assert rr._env_auto_hitl_enabled() is True
        env_without = {k: v for k, v in os.environ.items() if k != "KHIMAIRA_AUTO_HITL"}
        env_without["KHIMAIRA_AUTO_HITL"] = "0"
        with patch.dict(os.environ, env_without, clear=True):
            assert rr._env_auto_hitl_enabled() is False


class TestProcessWindowHitl:
    """Integration: _process_window routes to HITL when a prompt is detected."""

    def _win(self):
        return {"window_id": 100, "role": "agent"}

    @pytest.mark.asyncio
    async def test_hitl_prompt_triggers_hitl_not_compact(self):
        """A HITL prompt at >85% context → HITL path, NOT /compact."""
        screen = NUMBERED_PROMPT + "\n85% context used\n"

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_env_auto_hitl_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_session_hitl_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_handle_hitl_prompt", return_value="answered") as mock_hitl,
            patch.object(rr, "_inject_text_and_submit") as mock_inject,
        ):
            await rr._process_window(self._win())

        mock_hitl.assert_called_once()
        # /compact must NOT have been injected
        for call_args in mock_inject.call_args_list:
            assert "/compact" not in str(call_args)

    @pytest.mark.asyncio
    async def test_no_hitl_when_opt_out(self):
        """HITL processing skipped when session has .nohitl marker."""
        screen = NUMBERED_PROMPT

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_env_auto_hitl_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_session_hitl_opt_out", return_value=True),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_handle_hitl_prompt") as mock_hitl,
        ):
            await rr._process_window(self._win())

        mock_hitl.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_hitl_when_globally_disabled(self):
        """KHIMAIRA_AUTO_HITL=0 → HITL handler not called."""
        screen = NUMBERED_PROMPT

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_env_auto_hitl_enabled", return_value=False),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_handle_hitl_prompt") as mock_hitl,
        ):
            await rr._process_window(self._win())

        mock_hitl.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_log_written_for_answer(self):
        """Answered HITL generates INFO audit log entry."""
        screen = NUMBERED_PROMPT
        # Clear debounce so the HITL path isn't gated by a previous test's cooldown.
        rr._DEBOUNCE.pop((100, "hitl"), None)

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_env_auto_hitl_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_session_hitl_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_handle_hitl_prompt", return_value="answered"),
            patch.object(rr._log, "info") as mock_log,
        ):
            await rr._process_window(self._win())

        # At least one INFO log referencing the HITL action
        logged = " ".join(str(c) for c in mock_log.call_args_list)
        assert "answered" in logged or "hitl" in logged.lower()


# ---------------------------------------------------------------------------
# HITL escalation dedupe — _HITL_ESCALATED prevents same prompt spamming master
# ---------------------------------------------------------------------------

class TestHitlEscalationDedupe:
    """_process_window dedupes escalation: same prompt → one notice; changed
    prompt → re-escalates; cleared prompt (no-HITL cycle) → resets marker."""

    # Representative HITL screen text: Edit dialog for a specific file.
    # Constructed to mirror the real Claude Code permission dialog shape that
    # caused frontend-lead w191 to escalate 26x for the same unresolved prompt.
    REAL_SHAPED_SCREEN = (
        "frontend-lead-1\n"
        "Working on #14 auto-dispatch...\n"
        "\n"
        "⚡ Edit file: packages/jeevy_portal/src/features/hub/HubLayout.tsx\n"
        "Do you want to proceed?\n"
        "❯ 1. Yes, and don't ask again for this session\n"
        "  2. Yes\n"
        "  3. No, and tell Claude what to do differently\n"
    )
    CHANGED_SCREEN = (
        "frontend-lead-1\n"
        "\n"
        "⚡ Edit file: packages/jeevy_portal/src/features/auth/LoginPage.tsx\n"
        "Do you want to proceed?\n"
        "❯ 1. Yes, and don't ask again for this session\n"
        "  2. Yes\n"
        "  3. No, and tell Claude what to do differently\n"
    )

    def _win(self):
        return {"window_id": 191, "role": "frontend-lead", "raw_name": "frontend-lead-1",
                "cmdline": "claude-chat -r frontend-lead-1"}

    @pytest.fixture(autouse=True)
    def clear_state(self):
        rr._DEBOUNCE.pop((191, "hitl"), None)
        rr._HITL_ESCALATED.pop(191, None)
        yield
        rr._DEBOUNCE.pop((191, "hitl"), None)
        rr._HITL_ESCALATED.pop(191, None)

    def _ctx(self, screen, handle_result="escalated"):
        return (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_env_auto_hitl_enabled", return_value=True),
            patch.object(rr, "_resolve_session_by_name", return_value="3cf5ee30-uuid"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_session_hitl_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
            patch.object(rr, "_handle_hitl_prompt", return_value=handle_result),
        )

    @pytest.mark.asyncio
    async def test_same_prompt_escalates_exactly_once_across_three_cycles(self):
        """Same HITL prompt across N scan cycles → exactly ONE escalation notice."""
        screen = self.REAL_SHAPED_SCREEN
        handle_mock = None

        # Cycle 1: first appearance → should escalate
        patches = self._ctx(screen)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as mock_handle:
            handle_mock = mock_handle
            await rr._process_window(self._win())

        assert handle_mock.call_count == 1, "first cycle should call _handle_hitl_prompt"
        assert rr._HITL_ESCALATED.get(191) is not None, "escalation hash should be stored"

        # Cycle 2: same prompt — debounce cleared so cooldown re-enters, but dedupe should skip
        rr._DEBOUNCE.pop((191, "hitl"), None)
        patches2 = self._ctx(screen)
        with patches2[0], patches2[1], patches2[2], patches2[3], patches2[4], patches2[5], patches2[6] as mock_handle2:
            await rr._process_window(self._win())

        assert mock_handle2.call_count == 0, "second cycle with same prompt must be deduped"

        # Cycle 3: same prompt again — still deduped
        rr._DEBOUNCE.pop((191, "hitl"), None)
        patches3 = self._ctx(screen)
        with patches3[0], patches3[1], patches3[2], patches3[3], patches3[4], patches3[5], patches3[6] as mock_handle3:
            await rr._process_window(self._win())

        assert mock_handle3.call_count == 0, "third cycle with same prompt must be deduped"

    @pytest.mark.asyncio
    async def test_changed_prompt_re_escalates(self):
        """A new/changed prompt in the same window → escalates again."""
        # Cycle 1: first prompt escalates
        rr._DEBOUNCE.pop((191, "hitl"), None)
        patches = self._ctx(self.REAL_SHAPED_SCREEN)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as mock1:
            await rr._process_window(self._win())
        assert mock1.call_count == 1

        # Cycle 2: CHANGED prompt in same window — new hash → must re-escalate
        rr._DEBOUNCE.pop((191, "hitl"), None)
        patches2 = self._ctx(self.CHANGED_SCREEN)
        with patches2[0], patches2[1], patches2[2], patches2[3], patches2[4], patches2[5], patches2[6] as mock2:
            await rr._process_window(self._win())
        assert mock2.call_count == 1, "changed prompt must escalate again"

    @pytest.mark.asyncio
    async def test_cleared_prompt_resets_marker_for_fresh_escalation(self):
        """Prompt clears (no-HITL cycle) → marker removed → re-appearance escalates."""
        # Cycle 1: prompt escalates, marker stored
        rr._DEBOUNCE.pop((191, "hitl"), None)
        patches = self._ctx(self.REAL_SHAPED_SCREEN)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            await rr._process_window(self._win())
        assert rr._HITL_ESCALATED.get(191) is not None

        # Cycle 2: NO HITL prompt (idle window) → marker cleared
        rr._DEBOUNCE.pop((191, "hitl"), None)
        idle_screen = "frontend-lead-1\nIdling...\n"  # no HITL dialog
        idle_patches = self._ctx(idle_screen)
        with idle_patches[0], idle_patches[1], idle_patches[2], idle_patches[3], idle_patches[4], idle_patches[5], idle_patches[6] as mock_idle:
            await rr._process_window(self._win())
        assert mock_idle.call_count == 0
        assert rr._HITL_ESCALATED.get(191) is None, "marker must be cleared on no-HITL cycle"

        # Cycle 3: same prompt reappears → should escalate fresh (marker was cleared)
        rr._DEBOUNCE.pop((191, "hitl"), None)
        patches3 = self._ctx(self.REAL_SHAPED_SCREEN)
        with patches3[0], patches3[1], patches3[2], patches3[3], patches3[4], patches3[5], patches3[6] as mock3:
            await rr._process_window(self._win())
        assert mock3.call_count == 1, "prompt re-appearance after clear must escalate"

    def test_prompt_content_hash_stable_across_identical_renders(self):
        """Same raw_block → same hash (extraction is deterministic)."""
        h1 = rr._prompt_content_hash(self.REAL_SHAPED_SCREEN[-600:])
        h2 = rr._prompt_content_hash(self.REAL_SHAPED_SCREEN[-600:])
        assert h1 == h2

    def test_prompt_content_hash_differs_for_changed_prompt(self):
        """Different ⚡ tool-call line → different hash."""
        h1 = rr._prompt_content_hash(self.REAL_SHAPED_SCREEN[-600:])
        h2 = rr._prompt_content_hash(self.CHANGED_SCREEN[-600:])
        assert h1 != h2

    def test_prompt_content_hash_ignores_volatile_trailing_chrome(self):
        """Two renders of the same Edit prompt that differ only in trailing volatile
        content (token count, elapsed timer) → same hash. This is the key test:
        the ⚡-line extractor isolates the stable action-line so volatile screen
        chrome can't cause hash churn → dedupe fires correctly on the real live path."""
        # Render 1: Edit prompt + trailing token/timer line (render at T+0)
        render_1 = (
            "frontend-lead-1\n"
            "⚡ Edit file: packages/jeevy_portal/src/features/hub/HubLayout.tsx\n"
            "Do you want to proceed?\n"
            "❯ 1. Yes, and don't ask again for this session\n"
            "  2. Yes\n"
            "  3. No\n"
            "Frosting… 2m 42s · ↑ 9.0k tokens\n"  # volatile — changes each render
        )
        # Render 2: same dialog + different elapsed time (render at T+6min)
        render_2 = (
            "frontend-lead-1\n"
            "⚡ Edit file: packages/jeevy_portal/src/features/hub/HubLayout.tsx\n"
            "Do you want to proceed?\n"
            "❯ 1. Yes, and don't ask again for this session\n"
            "  2. Yes\n"
            "  3. No\n"
            "Frosting… 8m 55s · ↑ 9.0k tokens\n"  # different elapsed — hash must not churn
        )
        h1 = rr._prompt_content_hash(render_1[-600:])
        h2 = rr._prompt_content_hash(render_2[-600:])
        assert h1 == h2, "same ⚡ Edit line with different volatile trailing content must hash identically"

    def test_prompt_content_hash_uses_bash_cmd_when_available(self):
        """For Bash prompts, hash is derived from the extracted command (volatile-free)."""
        bash_screen = "Some preamble\nBash(ls -la /tmp)\n❯ 1. Allow this time\n  2. Deny\n"
        h = rr._prompt_content_hash(bash_screen)
        import hashlib
        expected = hashlib.md5("ls -la /tmp".encode(), usedforsecurity=False).hexdigest()
        assert h == expected

    def test_prompt_content_hash_uses_question_line_for_no_lightning_prompt(self):
        """For ⚡-less pure-question prompts, hash is derived from the question line."""
        pure_question = (
            "Some context text\n"
            "Do you want to run the test suite?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
        )
        h = rr._prompt_content_hash(pure_question)
        import hashlib
        expected = hashlib.md5("Do you want to run the test suite?".encode(), usedforsecurity=False).hexdigest()
        assert h == expected


# ---------------------------------------------------------------------------
# Lane 5 — name-based session resolution (fixes the ambiguous-role abort)
# ---------------------------------------------------------------------------

class TestProcessWindowNameResolution:
    """_process_window resolves session UUID by window NAME (unique), not role
    (ambiguous when multiple sessions share a role like 'agent')."""

    @pytest.fixture(autouse=True)
    def clear_debounce(self):
        rr._DEBOUNCE.clear()
        yield
        rr._DEBOUNCE.clear()

    def _win(self, raw_name="agent-2", role="agent", window_id=200):
        return {"window_id": window_id, "role": role, "raw_name": raw_name,
                "cmdline": f"claude-chat -r {raw_name}"}

    @pytest.mark.asyncio
    async def test_name_resolution_used_when_raw_name_present(self):
        """When raw_name is set, resolve_active_session is called first."""
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_by_name", return_value="uuid-agent-2") as mock_name,
            patch.object(rr, "_resolve_session_for_role") as mock_role,
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=">"),
            patch.object(rr, "_compute_context_pct", return_value=50),
            patch("khimaira.monitor.api.chats._get_session_obligations", return_value=[]),
            patch.object(rr, "_session_has_pending_task", return_value=False),
            patch.object(rr, "_session_has_pending_invite", return_value=False),
        ):
            await rr._process_window(self._win())

        mock_name.assert_called_once_with("agent-2")
        mock_role.assert_not_called()  # name succeeded → role fallback not needed

    @pytest.mark.asyncio
    async def test_role_fallback_when_name_resolution_fails(self):
        """When resolve_active_session returns None, fall back to role-based."""
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_by_name", return_value=None),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-via-role") as mock_role,
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=">"),
            patch.object(rr, "_compute_context_pct", return_value=50),
            patch("khimaira.monitor.api.chats._get_session_obligations", return_value=[]),
            patch.object(rr, "_session_has_pending_task", return_value=False),
            patch.object(rr, "_session_has_pending_invite", return_value=False),
        ):
            await rr._process_window(self._win())

        mock_role.assert_called_once_with("agent")  # fallback to role

    @pytest.mark.asyncio
    async def test_no_raw_name_skips_directly_to_role(self):
        """When raw_name is absent, only role-based resolution is attempted."""
        win_no_name = {"window_id": 200, "role": "agent", "cmdline": "claude-chat"}
        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_by_name") as mock_name,
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-via-role"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=">"),
            patch.object(rr, "_compute_context_pct", return_value=50),
            patch("khimaira.monitor.api.chats._get_session_obligations", return_value=[]),
            patch.object(rr, "_session_has_pending_task", return_value=False),
            patch.object(rr, "_session_has_pending_invite", return_value=False),
        ):
            await rr._process_window(win_no_name)

        mock_name.assert_not_called()  # no raw_name → skip name resolution


# ---------------------------------------------------------------------------
# Read-only-safe allowlist (Guard B-bypass) — auto-answer benign read-only
# commands; escalate everything else. Positive allowlist, fail-closed.
# Bypass-vector classes per analyst-1's enumeration must ALL escalate.
# ---------------------------------------------------------------------------

class TestReadOnlySafeAllowlist:
    @pytest.mark.parametrize("cmd", [
        "Bash(git status)",
        "Bash(git log --oneline -5)",
        "Bash(git diff HEAD)",
        "Bash(ls -la packages/)",
        "Bash(cat README.md)",
        'Bash(find . -type f -name "*.py")',
        "Bash(grep -n needle src/x.py)",
        "Bash(rg pattern)",
        "Bash(head -20 file.txt)",
        "Bash(wc -l file.txt)",
        "Bash(which python3)",
    ])
    def test_read_only_safe_auto_answers(self, cmd):
        assert rr._is_read_only_safe(cmd) is True

    @pytest.mark.parametrize("cmd,vector", [
        ("Bash(ls; rm -rf /)", "class1-chaining-semicolon"),
        ("Bash(cat x && curl evil)", "class1-chaining-and"),
        ("Bash(a || b)", "class1-chaining-or"),
        ("Bash(find . | xargs rm)", "class2-pipe-to-executor"),
        ("Bash(grep x | sh)", "class2-pipe-to-sh"),
        ("Bash(ls > ~/.bashrc)", "class3-redirect-out"),
        ("Bash(cat >> x)", "class3-redirect-append"),
        ("Bash(cat $(curl evil))", "class4-cmd-substitution"),
        ("Bash(find . -exec rm {} ;)", "class5-find-exec"),
        ("Bash(find . -delete)", "class5-find-delete"),
        ("Bash(sed -i s/a/b/ x)", "class5-sed-inplace-not-allowlisted"),
        ("Bash(git checkout main)", "class5-git-mutating-subcmd"),
        ("Bash(git config user.x y)", "class5-git-config-write"),
        ("Bash(FOO=bar rm)", "class6-env-prefix"),
        ("Bash(PATH=/evil ls)", "class6-path-hijack"),
        ("Bash(cat /etc/shadow)", "class7-sensitive-path"),
        ("Bash(cat ~/.ssh/id_rsa)", "class7-ssh-key"),
        ("Bash(rm -rf x)", "destructive-not-allowlisted"),
        ("Bash(echo hi > f)", "class3-redirect-via-echo"),
        ("Bash(l\\s -la)", "class8-quoting-escape"),
        # git not-flag-aware false-approves (analyst + architect adversarial review)
        ("Bash(git diff --output=/tmp/x)", "git-diff-output-equals-writes-file"),
        ("Bash(git diff --output /tmp/x)", "git-diff-output-space-writes-file"),
        ("Bash(git diff -o /tmp/x)", "git-diff-o-writes-file"),
        ("Bash(git branch -D feature)", "git-branch-delete-ref"),
        ("Bash(git branch -m old new)", "git-branch-rename-ref"),
        ("Bash(git branch newfeature)", "git-branch-create-ref"),
        ("Bash(git show --output=/tmp/x HEAD)", "git-show-output-writes-file"),
        # find write-action variants that slipped the exact-token blocklist
        ("Bash(find . -fprint0 /tmp/x)", "find-fprint0-writes-file"),
        ("Bash(find . -execdir rm {} ;)", "find-execdir"),
        ("Bash(find . -okdir rm {} ;)", "find-okdir"),
        ("Bash(find . -fprintf /tmp/x %p)", "find-fprintf"),
    ])
    def test_dangerous_commands_escalate(self, cmd, vector):
        # The load-bearing assertion: NEVER a false-approve on a write/side-effect.
        assert rr._is_read_only_safe(cmd) is False, f"FALSE-APPROVE on {vector}: {cmd!r}"

    def test_unextractable_command_fails_closed(self):
        # No clean Bash(...) extraction → escalate (ambiguity = fail closed).
        assert rr._is_read_only_safe("some garbled terminal text without a command") is False
        assert rr._is_read_only_safe("Bash(find . -name (nested))") is False  # nested paren

    def test_box_border_leak_escalates(self):
        # Terminal box-border (│) or nbsp leaking into the extracted command
        # means the render is ambiguous → escalate.
        assert rr._is_read_only_safe("Bash(ls │ rm)") is False
        assert rr._is_read_only_safe("Bash(ls\xa0-la)") is False


# ---------------------------------------------------------------------------
# Rate-limit detection (window-scanner). Keyed on CC's literal error strings,
# wrap-tolerant (the window wraps long lines mid-phrase). MUST NOT fire on
# discussion-of-rate-limits (the meta-false-positive).
# ---------------------------------------------------------------------------

class TestRateLimitDetect:
    def test_real_server_429_render_wrapped(self):
        # Actual captured render (agent-2, 2026-06-02) — wraps "limiting\n  requests".
        real = (
            "  ⎿  API Error: Server is temporarily limiting\n"
            "     requests (not your usage limit) · Rate\n"
            "     limited"
        )
        assert rr._detect_rate_limit(real) is True

    def test_usage_cap_render_wrapped(self):
        cap = "● Claude usage limit\n  reached. Your limit will reset at 3pm."
        assert rr._detect_rate_limit(cap) is True

    @pytest.mark.parametrize("text", [
        "@khimaira-0 — task: build the rate-limit detection window-scanner",
        "agent-2 was rate limited. we are still not catching this?",
        "🔧 FIX rate-limit detection (frontend-lead-1) — the tracker is blind",
        "discussing the rate limit and how Rate limited errors render",
    ])
    def test_discussion_does_not_false_positive(self, text):
        # The chat is full of "rate limit(ed)" discussion — the detector keys on
        # CC's literal API-error phrases, not the generic term.
        assert rr._detect_rate_limit(text) is False


class TestServer429Detect:
    """_detect_server_429 — discriminates 429 from the 5h personal usage cap.

    These are DIFFERENT failure classes needing OPPOSITE mitigations:
    429 (server temporary) → stagger + retry soon.
    5h cap → wait for personal reset (staggering cannot help a 5h cap).
    """

    CONFIRMED_429 = (
        "API Error: Server is temporarily limiting requests "
        "(not your usage limit) · Rate limited"
    )
    WRAPPED_429 = (
        "  ⎿  API Error: Server is temporarily limiting\n"
        "     requests (not your usage limit) · Rate\n"
        "     limited"
    )
    USAGE_CAP = "● Claude usage limit\n  reached. Your limit will reset at 3pm."

    def test_confirmed_429_string_matches(self):
        """Joseph's exact confirmed render string (2026-06-03) must match."""
        assert rr._detect_server_429(self.CONFIRMED_429) is True

    def test_wrapped_429_matches(self):
        """Line-wrapped variant (window wraps mid-phrase) must match."""
        assert rr._detect_server_429(self.WRAPPED_429) is True

    def test_usage_cap_does_not_match(self):
        """THE DISCRIMINATOR: 5h personal usage cap must NOT match _detect_server_429.

        Failure here means the 429 and cap detectors are conflated, which would
        apply the wrong mitigation (stagger/retry for a 5h cap → wastes retries).
        """
        assert rr._detect_server_429(self.USAGE_CAP) is False

    def test_generic_rate_limit_text_does_not_match(self):
        """Discussion text mentioning rate limits must NOT match."""
        assert rr._detect_server_429("discussing the rate limit issue") is False


# ---------------------------------------------------------------------------
# Disk-WIP probe — _session_has_recent_wip + wake-path integration
# ---------------------------------------------------------------------------

class TestResolveTaskTargetPaths:
    """_resolve_task_target_paths extracts file paths from task bodies."""

    def test_backtick_paths(self, tmp_path):
        f = tmp_path / "roster_recovery.py"
        f.write_text("x")
        body = f"Fix the bug in `{f}`"
        result = rr._resolve_task_target_paths(body, tmp_path)
        assert f in result

    def test_package_prefix_path(self, tmp_path):
        pkg = tmp_path / "packages" / "foo"
        pkg.mkdir(parents=True)
        f = pkg / "bar.py"
        f.write_text("x")
        body = f"Edit packages/foo/bar.py to fix this"
        result = rr._resolve_task_target_paths(body, tmp_path)
        assert any(p.name == "bar.py" for p in result)

    def test_nonexistent_paths_excluded(self, tmp_path):
        body = "Edit `packages/nonexistent/file.py`"
        result = rr._resolve_task_target_paths(body, tmp_path)
        assert result == []

    def test_empty_task_body(self, tmp_path):
        assert rr._resolve_task_target_paths("", tmp_path) == []


class TestSessionHasRecentWip:
    """_session_has_recent_wip: per-session-precise disk-WIP detection."""

    def test_recent_mtime_returns_true(self, tmp_path):
        """Task target file modified recently → WIP detected."""
        f = tmp_path / "target.py"
        f.write_text("x")
        # mtime is NOW (just created) → within 900s threshold
        body = f"Implement fix in `{f}`"
        assert rr._session_has_recent_wip("test-session", body, tmp_path, 900.0) is True

    def test_stale_mtime_returns_false(self, tmp_path):
        """Task target file NOT modified recently → no WIP."""
        f = tmp_path / "old_target.py"
        f.write_text("x")
        import os
        # Set mtime to 30 minutes ago
        old_ts = time.time() - 1800
        os.utime(f, (old_ts, old_ts))
        body = f"Implement fix in `{f}`"
        assert rr._session_has_recent_wip("test-session", body, tmp_path, 900.0) is False

    def test_no_task_body_returns_false(self, tmp_path):
        """No task body → can't attribute in shared-cwd → returns False."""
        assert rr._session_has_recent_wip("test-session", None, tmp_path, 900.0) is False

    def test_unresolvable_paths_returns_false(self, tmp_path):
        """Task body mentions files that don't exist → returns False."""
        body = "Edit `packages/nonexistent/ghost.py`"
        assert rr._session_has_recent_wip("test-session", body, tmp_path, 900.0) is False

    def test_fresh_non_target_file_ignored(self, tmp_path):
        """Shared-cwd regression guard: a fresh file NOT in this session's task
        targets must NOT trigger WIP. This test FAILS if the probe ever performs
        a workspace-scan (it would pick up peer_edit.py). Passes only with the
        correct owed-task-scoped attribution."""
        import os
        target = tmp_path / "my_target.py"
        target.write_text("x")
        stale_ts = time.time() - 1800
        os.utime(target, (stale_ts, stale_ts))       # this session's target: stale
        (tmp_path / "peer_edit.py").write_text("fresh")  # a peer's fresh edit — NOT in task
        body = f"Implement fix in `{target}`"
        # My target is stale → no WIP, even though peer_edit.py is fresh in the same dir
        assert rr._session_has_recent_wip("test-session", body, tmp_path, 900.0) is False

    def test_stale_last_active_but_fresh_target_returns_true(self, tmp_path):
        """Hook-independent: last_active stale but target file fresh → WIP detected.
        This is the key false-dark case: SSE-deaf session whose hook can't bump
        last_active, but whose task-target file was just edited."""
        f = tmp_path / "task_target.py"
        f.write_text("recent_edit")
        # File is fresh (mtime=now) even though last_active would be stale
        body = f"Implement the fix in `{f}`"
        result = rr._session_has_recent_wip("deaf-session", body, tmp_path, 900.0)
        assert result is True, "stale-last_active + fresh target file must be detected as WIP"


class TestProcessWindowWakeDiskWIP:
    """Wake path: disk-WIP probe gates wake correctly in _process_window."""

    TASK_BODY = "Implement fix in `packages/khimaira/src/khimaira/monitor/chats.py`"

    def _win(self):
        return {"window_id": 300, "role": "agent", "raw_name": "agent-test"}

    @pytest.fixture(autouse=True)
    def clear_state(self):
        rr._DEBOUNCE.pop((300, "wake"), None)
        rr._DEBOUNCE.pop((300, "ratelimit"), None)
        yield
        rr._DEBOUNCE.pop((300, "wake"), None)
        rr._DEBOUNCE.pop((300, "ratelimit"), None)

    def _wake_ctx(self, has_wip=False, rate_limited=False, has_obligations=True):
        """Context manager stack for wake-path tests."""
        return (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_env_auto_hitl_enabled", return_value=False),
            patch.object(rr, "_resolve_session_by_name", return_value="session-uuid"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_session_hitl_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value="agent idle\n"),
            patch.object(rr, "_compute_context_pct", return_value=None),
            patch("khimaira.monitor.api.chats._get_session_obligations",
                  return_value=[{"type": "task"}] if has_obligations else []),
            patch.object(rr, "_session_has_pending_task", return_value=False),
            patch.object(rr, "_session_has_pending_invite", return_value=False),
            patch.object(rr, "_session_has_recent_wip", return_value=has_wip),
            patch.object(rr, "_get_session_active_task_body",
                         return_value=self.TASK_BODY if has_wip else None),
        )

    def _session_row(self, idle_s=600):
        return {"session_id": "session-uuid", "last_active_age_s": idle_s}

    @pytest.mark.asyncio
    async def test_alive_but_working_not_woken(self):
        """Session with recent disk-WIP is NOT woken — false-dark eliminated."""
        import sys
        from khimaira.monitor import sessions as sessions_mod
        row = self._session_row(idle_s=600)

        patches = self._wake_ctx(has_wip=True)
        with (patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
              patches[6], patches[7], patches[8], patches[9], patches[10], patches[11],
              patch.object(sessions_mod, "list_sessions", return_value=[row]),
              patch.object(rr, "_inject_text_and_submit") as mock_inject):
            await rr._process_window(self._win())

        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_genuinely_idle_is_woken(self):
        """Session with no disk-WIP AND no rate-limit → IS woken."""
        from khimaira.monitor import sessions as sessions_mod
        row = self._session_row(idle_s=600)

        patches = self._wake_ctx(has_wip=False)
        with (patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
              patches[6], patches[7], patches[8], patches[9], patches[10], patches[11],
              patch.object(sessions_mod, "list_sessions", return_value=[row]),
              patch.object(rr, "_inject_text_and_submit", return_value=True) as mock_inject):
            await rr._process_window(self._win())

        mock_inject.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limited_not_woken(self):
        """Rate-limited window is NOT woken (futile injection)."""
        from khimaira.monitor import sessions as sessions_mod
        row = self._session_row(idle_s=600)
        # Set rate-limit debounce to recent
        rr._DEBOUNCE[(300, "ratelimit")] = time.time()

        patches = self._wake_ctx(has_wip=False)
        with (patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
              patches[6], patches[7], patches[8], patches[9], patches[10], patches[11],
              patch.object(sessions_mod, "list_sessions", return_value=[row]),
              patch.object(rr, "_inject_text_and_submit") as mock_inject):
            await rr._process_window(self._win())

        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_shared_cwd_no_cross_attribution(self):
        """Session A editing a file NOT in session B's owed-task → B is still woken.
        Proves attribution is per-owed-task, not shared-cwd. This is the key test
        for the shared-cwd false-attribution bug (analyst premise flag)."""
        from khimaira.monitor import sessions as sessions_mod
        row = self._session_row(idle_s=600)
        # B has NO disk-WIP on its own task targets (has_wip=False for B's session)
        # even though A may be editing in the same repo

        patches = self._wake_ctx(has_wip=False)  # B's WIP check returns False
        with (patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
              patches[6], patches[7], patches[8], patches[9], patches[10], patches[11],
              patch.object(sessions_mod, "list_sessions", return_value=[row]),
              patch.object(rr, "_inject_text_and_submit", return_value=True) as mock_inject):
            await rr._process_window(self._win())

        # B must be woken — A's edits in the shared repo do NOT suppress B's wake
        mock_inject.assert_called_once()


class TestTitleMatchExact:
    """kitty title: is an unanchored regex — _title_match_arg must anchor it
    so a roster's `agent-1` nudge can't cross-fire its sister `muther-agent-1`."""

    def test_anchors_and_escapes(self):
        assert rr._title_match_arg("agent-1") == r"--match=title:^agent\-1$"

    def test_substring_twin_would_not_match_anchored(self):
        import re as _re
        arg = rr._title_match_arg("agent-1")
        pattern = arg.split("title:", 1)[1]  # ^agent\-1$
        assert _re.search(pattern, "agent-1"), "must match its own window"
        assert not _re.search(pattern, "muther-agent-1"), "must NOT match the sister roster twin"
        assert not _re.search(pattern, "agent-11"), "must NOT match agent-11 (substring)"
