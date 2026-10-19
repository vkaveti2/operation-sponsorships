"""Keep every test independent of the real machine: no real login, OAuth client, brand list, lookup
memory or git settings can change a result. A test that wants one of them builds its own in tmp_path."""
import pytest

import src.cli as cli


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "TOKEN_FILE", tmp_path / "no-token.json")
    monkeypatch.setattr(cli, "CREDENTIALS_FILE", tmp_path / "no-credentials.json")
    monkeypatch.setattr(cli, "DEFAULT_TARGETS_CSV", tmp_path / "no-targets.csv")
    monkeypatch.setattr(cli, "_git_hooks_path", lambda: "scripts/hooks")
