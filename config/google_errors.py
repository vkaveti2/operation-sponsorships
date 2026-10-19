"""Turn raw Google API errors into one-line, fixable messages.

Works for google-api-python-client's HttpError (Gmail, Drive) and gspread's APIError (Sheets).
"""
from __future__ import annotations

import json
from typing import Any

API_LABELS = {"sheets": "Google Sheets", "gmail": "Gmail", "drive": "Google Drive"}
API_CONSOLE_IDS = {
    "sheets": "sheets.googleapis.com",
    "gmail": "gmail.googleapis.com",
    "drive": "drive.googleapis.com",
}

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"}
)


def parse_google_error(payload: Any) -> tuple[int, str, str]:
    """Return (http status, machine reason, human message) from a Google error body.

    Accepts a dict, JSON text/bytes, or just the inner "error" dict. Missing parts come back
    as 0 / "".
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return 0, "", payload.strip()[:300]
    if not isinstance(payload, dict):
        return 0, "", str(payload)[:300]

    err = payload.get("error", payload)
    if isinstance(err, str):  # OAuth style: {"error": "invalid_grant", "error_description": "..."}
        return 0, err, str(payload.get("error_description", ""))
    if not isinstance(err, dict):
        return 0, "", str(err)[:300]

    try:
        status = int(err.get("code") or 0)
    except (TypeError, ValueError):
        status = 0
    message = str(err.get("message") or "")
    reason = ""
    for key in ("errors", "details"):
        for item in err.get(key) or []:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item["reason"])
                break
        if reason:
            break
    if not reason:
        reason = str(err.get("status") or "")
    return status, reason, message


def describe_exception(exc: Exception) -> tuple[int, str, str]:
    """(status, reason, message) for an HttpError, a gspread APIError, or anything else."""
    content = getattr(exc, "content", None)  # googleapiclient.errors.HttpError
    if content is not None:
        status, reason, message = parse_google_error(content)
        if not status:
            status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
        return status, reason, message
    error = getattr(exc, "error", None)  # gspread.exceptions.APIError
    if isinstance(error, dict):
        status, reason, message = parse_google_error({"error": error})
        return status or int(getattr(exc, "code", 0) or 0), reason, message
    return 0, "", str(exc)


def is_retryable(status: int, reason: str = "") -> bool:
    return status in RETRYABLE_STATUS or reason in RATE_LIMIT_REASONS


def explain_google_error(api: str, status: int, reason: str, message: str) -> str:
    label = API_LABELS.get(api, api)
    api_id = API_CONSOLE_IDS.get(api, f"{api}.googleapis.com")
    enable_url = f"https://console.cloud.google.com/apis/library/{api_id}"
    text = f"{reason} {message}".lower()

    if status == 401 or reason in {"authError", "UNAUTHENTICATED"}:
        return f"{label} rejected the saved login. Run: python main.py auth"
    if (
        reason in {"accessNotConfigured", "SERVICE_DISABLED"}
        or "has not been used in project" in text
        or "api has not been enabled" in text
        or "is disabled" in text
    ):
        return (
            f"The {label} API is not enabled in your Google Cloud project. Enable it here, "
            f"wait a minute, then retry: {enable_url}"
        )
    if reason in {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"} or (
        "insufficient authentication scopes" in text
    ):
        return (
            f"The saved login is missing {label} permission. Run: python main.py auth "
            "and tick every permission box."
        )
    if reason in RATE_LIMIT_REASONS or status == 429:
        return f"{label} is rate limiting this account. Wait a few minutes and run it again."
    if status == 404:
        return (
            f"{label} could not find that item. Check the ID, and that the signed-in "
            "Google account can open it."
        )
    if status >= 500:
        return f"{label} had a server problem ({status}). Wait a minute and try again."
    if status == 403:
        return f"{label} refused access ({reason or 'forbidden'}): {message}".strip()
    return f"{label} error {status} {reason}: {message}".strip()


def summarize_exception(exc: Exception, api: str) -> str:
    """One-line, actionable text for an exception raised by a Google client library."""
    status, reason, message = describe_exception(exc)
    if not (status or reason):
        return f"{API_LABELS.get(api, api)} request failed: {message or exc.__class__.__name__}"
    return explain_google_error(api, status, reason, message)
