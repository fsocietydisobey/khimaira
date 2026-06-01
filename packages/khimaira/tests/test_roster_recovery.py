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

    @pytest.mark.asyncio
    async def test_watcher_wakes_session_with_pending_task(self):
        """Idle session with a pending-not-started task qualifies for wake."""
        screen = "75% context used\n>"  # below compact threshold, idle

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
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
        screen = "60% context used\n>"  # below compact threshold, idle

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
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
        screen = "60% context used\n>"

        with (
            patch.object(rr, "_env_enabled", return_value=True),
            patch.object(rr, "_resolve_session_for_role", return_value="uuid-1234"),
            patch.object(rr, "_session_opt_out", return_value=False),
            patch.object(rr, "_get_screen", return_value=screen),
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
