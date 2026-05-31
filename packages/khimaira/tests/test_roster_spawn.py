"""Tests for cli.roster spawn command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from khimaira.cli import roster as roster_cmd


# ---------------------------------------------------------------------------
# _infer_role
# ---------------------------------------------------------------------------

class TestInferRole:
    def test_strips_numeric_suffix(self):
        assert roster_cmd._infer_role("agent-5") == "agent"

    def test_strips_prefixed_numeric_suffix(self):
        assert roster_cmd._infer_role("jp-backend-lead-2") == "jp-backend-lead"

    def test_bare_role_unchanged(self):
        # e.g. "master" has no numeric suffix
        result = roster_cmd._infer_role("master")
        assert result == "master"


# ---------------------------------------------------------------------------
# _find_roster_tab
# ---------------------------------------------------------------------------

SAMPLE_LS_WITH_ROSTER = __import__("json").dumps([{
    "tabs": [
        {"id": 17, "title": "khimaira-roster", "windows": []},
        {"id": 5,  "title": "editor", "windows": []},
    ]
}])

SAMPLE_LS_NO_ROSTER = __import__("json").dumps([{
    "tabs": [
        {"id": 5, "title": "editor", "windows": []},
    ]
}])


class TestFindRosterTab:
    def test_finds_roster_tab(self):
        with patch.object(roster_cmd, "_kitty", return_value=SAMPLE_LS_WITH_ROSTER):
            assert roster_cmd._find_roster_tab() == 17

    def test_returns_none_when_no_roster_tab(self):
        with patch.object(roster_cmd, "_kitty", return_value=SAMPLE_LS_NO_ROSTER):
            assert roster_cmd._find_roster_tab() is None

    def test_returns_none_when_kitty_unavailable(self):
        with patch.object(roster_cmd, "_kitty", return_value=None):
            assert roster_cmd._find_roster_tab() is None


# ---------------------------------------------------------------------------
# _find_last_agent_window
# ---------------------------------------------------------------------------

SAMPLE_LS_WITH_AGENTS = __import__("json").dumps([{
    "tabs": [{
        "id": 17,
        "title": "khimaira-roster",
        "windows": [
            {"id": 185, "cmdline": ["bash", "-ic", "cd '/home/_3ntropy/dev/khimaira' && claude-chat -r agent-1 --model sonnet"]},
            {"id": 194, "cmdline": ["bash", "-ic", "cd '/home/_3ntropy/dev/khimaira' && claude-chat -r agent-3 --model sonnet"]},
            {"id": 195, "cmdline": ["bash", "-ic", "cd '/home/_3ntropy/dev/khimaira' && claude-chat -r agent-2 --model sonnet"]},
            {"id": 204, "cmdline": ["bash", "-ic", "cd '/home/_3ntropy/dev/khimaira' && claude-chat -n agent-4 --model sonnet"]},
            {"id": 186, "cmdline": ["bash", "-ic", "cd '/home/_3ntropy/dev/khimaira' && claude-chat -r khimaira-0 --model sonnet"]},
        ]
    }]
}])


class TestFindLastAgentWindow:
    def test_returns_highest_numbered_agent(self):
        with patch.object(roster_cmd, "_kitty", return_value=SAMPLE_LS_WITH_AGENTS):
            wid = roster_cmd._find_last_agent_window(17)
        assert wid == 204  # agent-4 is the highest

    def test_returns_none_when_no_agents(self):
        ls = __import__("json").dumps([{"tabs": [{"id": 17, "title": "khimaira-roster",
            "windows": [{"id": 1, "cmdline": ["bash", "-ic", "claude-chat -r khimaira-0"]}]}]}])
        with patch.object(roster_cmd, "_kitty", return_value=ls):
            assert roster_cmd._find_last_agent_window(17) is None


# ---------------------------------------------------------------------------
# spawn — new-session flag (NOT -r/--resume)
# ---------------------------------------------------------------------------

class TestSpawnNewSessionFlag:
    """Critical: spawn must use -n (new session), NOT -r (resume)."""

    def test_uses_dash_n_not_dash_r(self):
        """The kitty launch command must use -n <name>, never -r <name>."""
        captured_cmd: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "209\n"
            r.stderr = ""
            return r

        with (
            patch.object(roster_cmd, "_find_roster_tab", return_value=17),
            patch.object(roster_cmd, "_find_last_agent_window", return_value=204),
            patch.object(roster_cmd, "_wait_for_session", return_value="uuid-new"),
            patch.object(roster_cmd, "_integrate_session", return_value=None),
            patch("subprocess.run", side_effect=fake_run),
        ):
            import argparse
            args = argparse.Namespace(
                roster_action="spawn",
                role_name="agent-5",
                model="sonnet",
                effort="medium",
                chat_id="chat-fdf7c4cbd3bd",
                cwd="/home/_3ntropy/dev/khimaira",
                timeout=45.0,
                dry_run=False,
            )
            result = roster_cmd._spawn(args)

        assert result == 0
        assert captured_cmd, "subprocess.run was not called"
        cmd = captured_cmd[0]
        # The bash -ic string must contain '-n agent-5', NOT '-r agent-5'
        bash_cmd = " ".join(cmd)
        assert "-n agent-5" in bash_cmd, f"Expected '-n agent-5' in command: {bash_cmd}"
        assert "-r agent-5" not in bash_cmd, f"'-r agent-5' (resume) must NOT appear: {bash_cmd}"

    def test_dry_run_shows_n_flag(self):
        """--dry-run output shows the -n flag in the kitty command."""
        import io
        from contextlib import redirect_stdout

        with (
            patch.object(roster_cmd, "_find_roster_tab", return_value=17),
            patch.object(roster_cmd, "_find_last_agent_window", return_value=204),
        ):
            import argparse
            args = argparse.Namespace(
                roster_action="spawn",
                role_name="agent-5",
                model="sonnet",
                effort="medium",
                chat_id="chat-fdf7c4cbd3bd",
                cwd="/dev/khimaira",
                timeout=45.0,
                dry_run=True,
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = roster_cmd._spawn(args)

        assert result == 0
        output = buf.getvalue()
        assert "-n agent-5" in output
        assert "-r agent-5" not in output


# ---------------------------------------------------------------------------
# spawn — auto-join (integration flow)
# ---------------------------------------------------------------------------

class TestSpawnAutoJoin:
    def test_auto_join_called_on_successful_registration(self):
        """spawn calls _integrate_session when the session registers."""
        with (
            patch.object(roster_cmd, "_find_roster_tab", return_value=17),
            patch.object(roster_cmd, "_find_last_agent_window", return_value=204),
            patch.object(roster_cmd, "_wait_for_session", return_value="uuid-5678"),
            patch.object(roster_cmd, "_integrate_session") as mock_integrate,
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="210\n", stderr="")
            import argparse
            args = argparse.Namespace(
                roster_action="spawn",
                role_name="agent-5",
                model="sonnet",
                effort="medium",
                chat_id="chat-fdf7c4cbd3bd",
                cwd="/dev/khimaira",
                timeout=30.0,
                dry_run=False,
            )
            result = roster_cmd._spawn(args)

        assert result == 0
        mock_integrate.assert_called_once_with(
            "uuid-5678", "agent", "chat-fdf7c4cbd3bd"
        )

    def test_spawn_fails_gracefully_when_no_roster_tab(self):
        """spawn returns 1 with helpful error if no khimaira-roster tab exists."""
        with patch.object(roster_cmd, "_find_roster_tab", return_value=None):
            import argparse
            args = argparse.Namespace(
                roster_action="spawn",
                role_name="agent-5",
                model="sonnet",
                effort="medium",
                chat_id="chat-fdf7c4cbd3bd",
                cwd="/dev/khimaira",
                timeout=30.0,
                dry_run=False,
            )
            result = roster_cmd._spawn(args)

        assert result == 1

    def test_spawn_partial_success_when_session_doesnt_register(self):
        """spawn returns 0 but warns when session doesn't register in time."""
        with (
            patch.object(roster_cmd, "_find_roster_tab", return_value=17),
            patch.object(roster_cmd, "_find_last_agent_window", return_value=204),
            patch.object(roster_cmd, "_wait_for_session", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="211\n", stderr="")
            import argparse
            args = argparse.Namespace(
                roster_action="spawn",
                role_name="agent-5",
                model="sonnet",
                effort="medium",
                chat_id="chat-fdf7c4cbd3bd",
                cwd="/dev/khimaira",
                timeout=0.1,
                dry_run=False,
            )
            result = roster_cmd._spawn(args)

        # Window launched = 0 (partial success; integration skipped)
        assert result == 0
