"""Exception hierarchy. main.py turns any OutreachError into a clean one-line message."""
from __future__ import annotations


class OutreachError(Exception):
    """Base class for every expected, user-fixable failure in this project."""


class ConfigError(OutreachError):
    """Missing or invalid configuration (.env values, credential files)."""


class AuthError(OutreachError):
    """Google OAuth problem: no token, expired token, missing scopes, wrong account."""


class SheetError(OutreachError):
    """The Google Sheet is missing, unreadable, or has an unexpected structure."""


class RowNotFound(SheetError):
    """A Row ID no longer exists in the sheet (deleted by a human mid-run)."""


class LLMError(OutreachError):
    """Base class for drafting-model failures."""


class LLMAuthError(LLMError):
    """The LLM API key is missing or rejected. Retrying cannot help."""


class LLMTransientError(LLMError):
    """Rate limit, overload, timeout, or network failure that survived retries."""


class LLMRefusal(LLMError):
    """The model declined to produce a draft."""


class DiscoveryError(OutreachError):
    """Lead discovery failed (bad key, bad input, unreadable CSV)."""


class QuotaExceeded(DiscoveryError):
    """The lead-discovery plan has no credits left. Stop the batch, keep what was found."""


class MailError(OutreachError):
    """Gmail failure that should stop the run (wrong account, quota, auth)."""


class RowChanged(SheetError):
    """A row was edited by a human after we read it, so the write was skipped."""
