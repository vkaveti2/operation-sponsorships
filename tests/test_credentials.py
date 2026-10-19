import json
import stat
from datetime import datetime, timedelta, timezone

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

import config.credentials as cred
from config.errors import AuthError
from config.settings import GOOGLE_SCOPES


def _creds(valid=True, scopes=None):
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1 if valid else -1)
    return Credentials(
        token="access", refresh_token="refresh", client_id="cid", client_secret="secret",
        token_uri="https://oauth2.googleapis.com/token", scopes=scopes or GOOGLE_SCOPES, expiry=expiry,
    )


def _write_token(path, scopes=None, refresh="refresh"):
    info = {"token": "a", "refresh_token": refresh, "client_id": "c", "client_secret": "s",
            "scopes": scopes if scopes is not None else GOOGLE_SCOPES}
    if refresh is None:
        info.pop("refresh_token")
    path.write_text(json.dumps(info))


def test_scope_helpers():
    assert cred.scope_set(None) == set() and cred.scope_set("a b") == {"a", "b"} and cred.scope_set(["a"]) == {"a"}
    assert cred.missing_scopes(GOOGLE_SCOPES) == []
    assert cred.missing_scopes(GOOGLE_SCOPES[:1]) == GOOGLE_SCOPES[1:]


@pytest.mark.parametrize("payload,needle", [
    (None, "missing"),
    ("{not json", "not valid JSON"),
    ('{"type": "service_account"}', "service account"),
    ('{"web": {"client_id": "x", "client_secret": "y"}}', "Web application"),
    ('{"installed": {"client_id": "x"}}', "client_secret"),
])
def test_client_file_problems(tmp_path, payload, needle):
    path = tmp_path / "credentials.json"
    if payload is not None:
        path.write_text(payload)
    assert needle in cred.client_file_problem(path)


def test_client_file_ok(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text('{"installed": {"client_id": "x.apps.googleusercontent.com", "client_secret": "y"}}')
    assert cred.client_file_problem(path) is None


def test_token_problems(tmp_path):
    path = tmp_path / "token.json"
    assert "python main.py auth" in cred.token_problem(path)
    path.write_text("garbage")
    assert "unreadable" in cred.token_problem(path)
    _write_token(path, refresh=None)
    assert "no refresh token" in cred.token_problem(path)
    _write_token(path, scopes=GOOGLE_SCOPES[:1])
    assert "Gmail" in cred.token_problem(path)
    _write_token(path)
    assert cred.token_problem(path) is None


def test_save_credentials_is_private_and_roundtrips(tmp_path):
    path = tmp_path / "sub" / "token.json"
    cred.save_credentials(_creds(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))
    loaded = Credentials.from_authorized_user_file(str(path))
    assert loaded.refresh_token == "refresh" and set(loaded.scopes) == set(GOOGLE_SCOPES)


def test_load_without_token_explains_next_step(tmp_path):
    with pytest.raises(AuthError, match="python main.py auth"):
        cred.load_credentials(tmp_path / "token.json")


def test_load_valid_token_makes_no_network_call(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    cred.save_credentials(_creds(valid=True), path)
    monkeypatch.setattr(Credentials, "refresh", lambda *a, **k: pytest.fail("should not refresh"))
    assert cred.load_credentials(path).refresh_token == "refresh"


def test_load_refreshes_expired_token_and_saves_it(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    cred.save_credentials(_creds(valid=False), path)

    def fake_refresh(self, request):
        self.token = "new-access"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = cred.load_credentials(path)
    assert creds.token == "new-access"
    assert json.loads(path.read_text())["token"] == "new-access"


def test_revoked_or_expired_login_is_explained(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    cred.save_credentials(_creds(valid=False), path)

    def boom(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", boom)
    with pytest.raises(AuthError, match="7 days"):
        cred.load_credentials(path)


class _FakeFlow:
    def __init__(self, creds):
        self._creds = creds

    def run_local_server(self, **kwargs):
        assert kwargs["access_type"] == "offline" and kwargs["prompt"] == "consent"
        assert kwargs["login_hint"] == "pennstatemcvb@gmail.com"
        return self._creds


def _client_file(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text('{"installed": {"client_id": "x", "client_secret": "y"}}')
    return path


def _patch_flow(monkeypatch, creds):
    monkeypatch.setattr(cred.InstalledAppFlow, "from_client_secrets_file",
                        classmethod(lambda cls, *a, **k: _FakeFlow(creds)))


def test_auth_flow_saves_token_for_the_right_account(tmp_path, monkeypatch):
    _patch_flow(monkeypatch, _creds())
    monkeypatch.setattr(cred, "account_email", lambda c: "pennstatemcvb@gmail.com")
    token = tmp_path / "token.json"
    cred.run_auth_flow("PennStateMCVB@gmail.com", credentials_file=_client_file(tmp_path), token_file=token)
    assert token.exists() and cred.token_problem(token) is None


def test_auth_flow_refuses_the_wrong_account_and_saves_nothing(tmp_path, monkeypatch):
    _patch_flow(monkeypatch, _creds())
    monkeypatch.setattr(cred, "account_email", lambda c: "varun.personal@gmail.com")
    token = tmp_path / "token.json"
    with pytest.raises(AuthError, match="varun.personal@gmail.com.*pennstatemcvb@gmail.com"):
        cred.run_auth_flow("pennstatemcvb@gmail.com", credentials_file=_client_file(tmp_path), token_file=token)
    assert not token.exists()


def test_auth_flow_rejects_partial_permissions(tmp_path, monkeypatch):
    partial = _creds(scopes=GOOGLE_SCOPES[:1])
    _patch_flow(monkeypatch, partial)
    monkeypatch.setattr(cred, "account_email", lambda c: pytest.fail("must fail before the account check"))
    token = tmp_path / "token.json"
    with pytest.raises(AuthError, match="not granted.*Gmail"):
        cred.run_auth_flow("pennstatemcvb@gmail.com", credentials_file=_client_file(tmp_path), token_file=token)
    assert not token.exists()


def test_auth_flow_needs_the_client_file(tmp_path):
    with pytest.raises(AuthError, match="Desktop app"):
        cred.run_auth_flow("pennstatemcvb@gmail.com", credentials_file=tmp_path / "nope.json", token_file=tmp_path / "t.json")


def test_relaxed_scope_env_is_restored(monkeypatch):
    monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)
    import os
    with cred._relaxed_scope_check():
        assert os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] == "1"
    assert "OAUTHLIB_RELAX_TOKEN_SCOPE" not in os.environ
