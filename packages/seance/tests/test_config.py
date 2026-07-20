from __future__ import annotations

import pytest
from click.testing import CliRunner
from seance.cli import main
from seance.config import SeanceConfigError, load_config


def test_missing_google_api_key_raises_typed_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)

    with pytest.raises(SeanceConfigError, match="GOOGLE_AI_API_KEY is not set"):
        load_config()


def test_cli_translates_config_error_to_process_exit(monkeypatch):
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)

    result = CliRunner().invoke(main, ["list"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "GOOGLE_AI_API_KEY is not set" in str(result.exception)
