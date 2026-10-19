"""Google OAuth for Sheets and Gmail from a single browser login.

    config/credentials.json   OAuth client you download from Google Cloud (Desktop app)
          |  python main.py auth   (browser opens, sign in as the club account)
          v
    config/token.json         refresh token, gitignored, chmod 600

Every later command calls load_credentials(), which refreshes the short-lived access token
silently. Problems raise AuthError with the exact fix.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Iterator

import gspread
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

from config.errors import AuthError
from config.google_errors import summarize_exception
from config.settings import CREDENTIALS_FILE, GOOGLE_SCOPES, TOKEN_FILE

log = logging.getLogger(__name__)

AUTH_TIMEOUT_SECONDS = 300
_SCOPE_LABELS = {
    "https://www.googleapis.com/auth/spreadsheets": "Google Sheets",
    "https://www.googleapis.com/auth/drive.file": "Google Drive (files this app creates)",
    "https://www.googleapis.com/auth/gmail.compose": "Gmail (create drafts and send)",
}


# --------------------------------------------------------------------------- scope helpers
def scope_set(value: Any) -> set[str]:
    """Normalise scopes given as None, 'a b c', or an iterable into a set."""
    if not value:
        return set()
    if isinstance(value, str):
        return set(value.split())
    return {str(item) for item in value}


def missing_scopes(have: Any, required: Iterable[str] = GOOGLE_SCOPES) -> list[str]:
    have_set = scope_set(have)
    return [scope for scope in required if scope not in have_set]


def _scope_names(scopes: Iterable[str]) -> str:
    return ", ".join(_SCOPE_LABELS.get(scope, scope) for scope in scopes)


# --------------------------------------------------------------------------- file checks
def client_file_problem(path: Path = CREDENTIALS_FILE) -> str | None:
    """None if the OAuth client file looks usable, else a one-line fix."""
    if not path.exists():
        return (
            f"{_display(path)} is missing. Create an OAuth client of type 'Desktop app' in "
            "Google Cloud Console, download the JSON, and save it there (README step 3)."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return f"{_display(path)} is not valid JSON. Download the OAuth client file again."
    if not isinstance(data, dict):
        return f"{_display(path)} is not an OAuth client file. Download it again."
    if data.get("type") == "service_account":
        return (
            f"{_display(path)} is a service account key. This project signs in as a person: "
            "create an OAuth client of type 'Desktop app' instead."
        )
    if "web" in data and "installed" not in data:
        return (
            f"{_display(path)} is a 'Web application' client, which cannot do the local "
            "browser sign-in. Create a new OAuth client of type 'Desktop app'."
        )
    installed = data.get("installed")
    if not isinstance(installed, dict) or not installed.get("client_id") or not installed.get("client_secret"):
        return f"{_display(path)} is missing client_id or client_secret. Download it again."
    return None


def token_problem(path: Path = TOKEN_FILE) -> str | None:
    """Offline check of the saved login. None means it is present and has every scope."""
    if not path.exists():
        return "Not signed in to Google yet. Run: python main.py auth"
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError):
        return f"{_display(path)} is unreadable. Delete it and run: python main.py auth"
    if not isinstance(info, dict) or not info.get("refresh_token"):
        return f"{_display(path)} has no refresh token. Delete it and run: python main.py auth"
    absent = missing_scopes(info.get("scopes"))
    if absent:
        return (
            f"The saved login was created without: {_scope_names(absent)}. "
            "Run: python main.py auth"
        )
    return None


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- saving
def save_credentials(creds: Credentials, path: Path = TOKEN_FILE) -> None:
    """Write token.json atomically, readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(creds.to_json())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------- loading
def load_credentials(token_file: Path = TOKEN_FILE) -> Credentials:
    """Return valid credentials, refreshing the access token when needed."""
    problem = token_problem(token_file)
    if problem:
        raise AuthError(problem)
    try:
        creds = Credentials.from_authorized_user_file(str(token_file))
    except (ValueError, OSError) as exc:
        raise AuthError(
            f"{_display(token_file)} could not be read ({exc}). Delete it and run: python main.py auth"
        ) from exc

    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise AuthError(
                "Google rejected the saved login (expired or revoked). If the OAuth consent "
                "screen is still in 'Testing', Google expires logins after 7 days; publish the "
                "app to stop that (README). Fix now with: python main.py auth"
            ) from exc
        except TransportError as exc:
            raise AuthError(
                "Could not reach Google to refresh the login. Check your internet connection "
                "and try again."
            ) from exc
        save_credentials(creds, token_file)
        log.debug("Refreshed Google access token")
    return creds


# --------------------------------------------------------------------------- clients
def build_service(api: str, version: str, creds: Credentials) -> Any:
    """googleapiclient service object (bundled discovery docs: no extra network call)."""
    return build(api, version, credentials=creds, cache_discovery=False)


def get_gspread_client(creds: Credentials) -> gspread.Client:
    client = gspread.authorize(creds)
    client.set_timeout((10, 60))  # (connect, read) seconds so a stalled call cannot hang forever
    return client


def account_email(creds: Credentials) -> str:
    """Address of the signed-in Gmail account, lower-cased."""
    try:
        profile = build_service("gmail", "v1", creds).users().getProfile(userId="me").execute()
    except HttpError as exc:
        raise AuthError(summarize_exception(exc, "gmail")) from exc
    return str(profile.get("emailAddress", "")).strip().lower()


# --------------------------------------------------------------------------- sign in
@contextlib.contextmanager
def _relaxed_scope_check() -> Iterator[None]:
    """Let oauthlib accept a partial grant so our own check can explain exactly what is missing."""
    previous = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
        else:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous


def run_auth_flow(
    expected_email: str,
    *,
    credentials_file: Path = CREDENTIALS_FILE,
    token_file: Path = TOKEN_FILE,
    open_browser: bool = True,
    timeout_seconds: int = AUTH_TIMEOUT_SECONDS,
) -> Credentials:
    """Browser sign-in. Saves token.json only if every permission was granted and the
    signed-in account is the expected one."""
    expected = expected_email.strip().lower()
    problem = client_file_problem(credentials_file)
    if problem:
        raise AuthError(problem)

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes=GOOGLE_SCOPES)
    try:
        with _relaxed_scope_check():
            creds = flow.run_local_server(
                port=0,
                open_browser=open_browser,
                timeout_seconds=timeout_seconds,
                authorization_prompt_message=(
                    "A browser window should open. If it does not, open this link and sign in "
                    "as {email}:\n{{url}}\n".format(email=expected)
                ),
                success_message="Signed in. You can close this tab and return to the terminal.",
                access_type="offline",
                prompt="consent",  # guarantees a refresh token even on re-auth
                login_hint=expected,
            )
    except WSGITimeoutError as exc:
        raise AuthError(
            f"No sign-in arrived within {timeout_seconds // 60} minutes. Run it again."
        ) from exc
    except OAuth2Error as exc:
        raise AuthError(
            f"Google sign-in did not complete ({exc.description or exc.error}). "
            "Run it again and click Allow."
        ) from exc

    absent = missing_scopes(creds.granted_scopes if creds.granted_scopes else creds.scopes)
    if absent:
        raise AuthError(
            f"Sign-in finished but these permissions were not granted: {_scope_names(absent)}. "
            "Run it again and tick every box on the consent screen."
        )
    if not creds.refresh_token:
        raise AuthError(
            "Google did not return a refresh token. Remove this app at "
            "https://myaccount.google.com/permissions and run it again."
        )

    actual = account_email(creds)
    if actual != expected:
        raise AuthError(
            f"You signed in as {actual or 'an unknown account'}, but this project sends from "
            f"{expected}. Nothing was saved. Run it again and choose {expected}."
        )

    save_credentials(creds, token_file)
    log.info("Signed in as %s. Login saved to %s", actual, _display(token_file))
    return creds
