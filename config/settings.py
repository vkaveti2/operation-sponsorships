"""Central configuration: paths, sender identity, hard limits, sheet schema, env loading.

Everything the rest of the project treats as a rule lives here so it is easy to audit.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

from config.errors import ConfigError

# --------------------------------------------------------------------------- paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
ENV_FILE = PROJECT_ROOT / ".env"
ENV_TEMPLATE = PROJECT_ROOT / ".env.template"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"  # OAuth client downloaded from Google Cloud
TOKEN_FILE = CONFIG_DIR / "token.json"  # created by `python main.py auth`

# --------------------------------------------------------------------------- hard limits
# These are rules, not preferences. Environment variables can lower them, never raise them.
HARD_CAP_PER_RUN = 15
MIN_DELAY_SECONDS = 180
MAX_DELAY_SECONDS = 420

# --------------------------------------------------------------------------- google
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",  # read and write the review queue
    "https://www.googleapis.com/auth/drive.file",  # create the sheet and share it
    "https://www.googleapis.com/auth/gmail.compose",  # create drafts, send, read own address
]

# --------------------------------------------------------------------------- sheet schema
class Col:
    ROW_ID = "Row ID"
    COMPANY = "Company Name"
    CATEGORY = "Category"
    TARGET_NAME = "Target Name"
    TARGET_EMAIL = "Target Email"
    HOOK = "Custom Hook / Angle"
    SUBJECT = "Generated Subject"
    BODY = "Generated Body"
    APPROVAL = "Approval Status"
    SEND = "Send Status"
    SENT_AT = "Sent Timestamp"
    GMAIL_ID = "Gmail ID"
    NOTES = "Notes"
    DOMAIN = "Company Domain"


COLUMNS = [
    Col.ROW_ID, Col.COMPANY, Col.CATEGORY, Col.TARGET_NAME, Col.TARGET_EMAIL, Col.HOOK,
    Col.SUBJECT, Col.BODY, Col.APPROVAL, Col.SEND, Col.SENT_AT, Col.GMAIL_ID, Col.NOTES,
    Col.DOMAIN,
]


class Approval:
    PENDING = "Pending"  # needs a draft
    PENDING_APPROVAL = "Pending Approval"  # draft written, waiting for a human
    APPROVED = "Approved"
    REJECTED = "Rejected"
    ALL = [PENDING, PENDING_APPROVAL, APPROVED, REJECTED]


class SendStatus:
    UNSENT = "Unsent"
    SENDING = "Sending"  # in flight; never retried automatically
    DRAFTED = "Drafted"  # Gmail draft created, a human presses Send
    SENT = "Sent"
    FAILED = "Failed"
    ALL = [UNSENT, SENDING, DRAFTED, SENT, FAILED]


CATEGORIES = ["Energy", "Skincare", "Hydration", "Recovery", "Gear"]

# Roles worth emailing, best first. Used by discovery for search filters and ranking.
TARGET_TITLES = [
    "Campus Marketing Manager",
    "Field Marketing Manager",
    "Sports Marketing Specialist",
    "Grassroots Marketing Coordinator",
    "Partnerships Manager",
]

# --------------------------------------------------------------------------- pitch rules
REQUIRED_PARTNER_LINE = (
    "We have partnered with brands in the past and are always excited to showcase "
    "and actively promote products our athletes love."
)
OPT_OUT_LINE = "If you'd rather not hear from us, just reply and I won't follow up again."

# What the drafter opens with, per category (a Custom Hook / Angle cell overrides these).
CATEGORY_ANGLES = {
    "Skincare": (
        "skin barrier health and sun protection through long outdoor tournament days"
    ),
    "Hydration": (
        "electrolytes and hydration across tournament weekends with several matches a day"
    ),
    "Recovery": (
        "recovery between matches and across tournament weekends, when shoulders, knees, "
        "and backs take the load"
    ),
    "Energy": "high-caffeine energy for early practices and heavy training blocks",
    "Gear": "equipment that holds up through a full season of practices and tournaments",
}
GENERIC_ANGLE = "what athletes need to perform and recover during a long competitive season"


@dataclass(frozen=True)
class Season:
    key: str
    phrase: str


def season_context(today: date) -> Season:
    """Season framing that stays true all year. Oct 18 gives the fall-wrap-up wording."""
    month = today.month
    if month in (8, 9):
        return Season(
            "early_fall",
            "we are in the middle of our fall tournament slate and building toward a full "
            "spring competitive run",
        )
    if month in (10, 11, 12):
        return Season(
            "late_fall",
            "we are wrapping up our fall tournament slate and gearing up for our full "
            "spring competitive run",
        )
    if month in (1, 2, 3, 4):
        return Season(
            "spring",
            "we are in the middle of our spring season, with regional tournaments and the "
            "national championship ahead",
        )
    return Season(
        "offseason",
        "we are coming off our spring season and already planning for next season",
    )


# --------------------------------------------------------------------------- sender
@dataclass(frozen=True)
class Sender:
    name: str = "Varun Kaveti"
    title_line: str = "President, Penn State Men's Club Volleyball"
    email: str = "pennstatemcvb@gmail.com"
    team_instagram: str = "@psumensclubvolleyball"
    personal_instagram: str = "@varunkaveti"

    @property
    def from_header(self) -> str:
        return f"{self.name} <{self.email}>"

    def signature(self) -> str:
        return "\n".join(
            [
                self.name,
                self.title_line,
                self.email,
                f"Team Instagram: {self.team_instagram}",
                f"Personal Instagram: {self.personal_instagram}",
            ]
        )


# --------------------------------------------------------------------------- settings
def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


_SHEET_URL_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def parse_spreadsheet_id(value: str) -> str:
    """Accept either a bare spreadsheet ID or a full sheet URL."""
    value = (value or "").strip()
    match = _SHEET_URL_ID.search(value)
    return match.group(1) if match else value


@dataclass(frozen=True)
class Settings:
    sender: Sender
    llm_provider: str = "anthropic"
    anthropic_api_key: str = field(default="", repr=False)
    anthropic_model: str = "claude-opus-5-5"
    anthropic_refusal_fallback: bool = True
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.8-flash"
    spreadsheet_id: str = ""
    worksheet_name: str = "Outreach Queue"
    share_sheet_with: tuple[str, ...] = ()
    discovery_backend: str = "hunter"
    hunter_api_key: str = field(default="", repr=False)
    apollo_api_key: str = field(default="", repr=False)
    banned_brands: tuple[str, ...] = ("Celsius",)
    max_per_run: int = HARD_CAP_PER_RUN

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None, *, env_file: Path | None = ENV_FILE) -> "Settings":
        """Read .env (real environment variables win) and build validated settings."""
        if env is None:
            if env_file is not None and Path(env_file).exists():
                load_dotenv(env_file, override=False)
            env = os.environ

        def get(key: str, default: str = "") -> str:
            value = env.get(key)
            return default if value is None or value.strip() == "" else value.strip()

        defaults = Sender()
        sender = Sender(
            name=get("SENDER_NAME", defaults.name),
            title_line=get("SENDER_TITLE", defaults.title_line),
            email=get("SENDER_EMAIL", defaults.email),
            team_instagram=get("TEAM_INSTAGRAM", defaults.team_instagram),
            personal_instagram=get("PERSONAL_INSTAGRAM", defaults.personal_instagram),
        )

        provider = get("LLM_PROVIDER", "anthropic").lower()
        if provider not in {"anthropic", "gemini"}:
            raise ConfigError(f"LLM_PROVIDER must be 'anthropic' or 'gemini', got '{provider}'.")

        backend = get("DISCOVERY_BACKEND", "hunter").lower()
        if backend not in {"hunter", "apollo"}:
            raise ConfigError(f"DISCOVERY_BACKEND must be 'hunter' or 'apollo', got '{backend}'.")

        try:
            requested = int(get("MAX_EMAILS_PER_RUN", str(HARD_CAP_PER_RUN)))
        except ValueError as exc:
            raise ConfigError("MAX_EMAILS_PER_RUN must be a whole number.") from exc
        max_per_run = max(1, min(requested, HARD_CAP_PER_RUN))  # can lower the cap, never raise it

        return cls(
            sender=sender,
            llm_provider=provider,
            anthropic_api_key=get("ANTHROPIC_API_KEY"),
            anthropic_model=get("ANTHROPIC_MODEL", "claude-opus-5-5"),
            anthropic_refusal_fallback=_bool(env.get("ANTHROPIC_REFUSAL_FALLBACK"), True),
            gemini_api_key=get("GEMINI_API_KEY"),
            gemini_model=get("GEMINI_MODEL", "gemini-3.8-flash"),
            spreadsheet_id=parse_spreadsheet_id(get("SPREADSHEET_ID")),
            worksheet_name=get("WORKSHEET_NAME", "Outreach Queue"),
            share_sheet_with=_csv(env.get("SHARE_SHEET_WITH")),
            discovery_backend=backend,
            hunter_api_key=get("HUNTER_API_KEY"),
            apollo_api_key=get("APOLLO_API_KEY"),
            banned_brands=_csv(env.get("BANNED_BRANDS")) or ("Celsius",),
            max_per_run=max_per_run,
        )

    # ---- requirement checks: each raises ConfigError with the exact fix
    def require_llm_key(self) -> str:
        if self.llm_provider == "anthropic":
            if not self.anthropic_api_key:
                raise ConfigError(
                    "ANTHROPIC_API_KEY is empty. Run `python main.py configure`, or set "
                    "LLM_PROVIDER=gemini and add GEMINI_API_KEY."
                )
            return self.anthropic_api_key
        if not self.gemini_api_key:
            raise ConfigError(
                "GEMINI_API_KEY is empty. Run `python main.py configure`, or set "
                "LLM_PROVIDER=anthropic and add ANTHROPIC_API_KEY."
            )
        return self.gemini_api_key

    def require_sheet(self) -> str:
        if not self.spreadsheet_id:
            raise ConfigError(
                "SPREADSHEET_ID is empty. Run `python main.py init-sheet` to create the "
                "review sheet (it saves the ID to .env), or paste an existing sheet URL into .env."
            )
        return self.spreadsheet_id

    def discovery_key(self, backend: str | None = None) -> str:
        backend = backend or self.discovery_backend
        key = self.hunter_api_key if backend == "hunter" else self.apollo_api_key
        if not key:
            name = "HUNTER_API_KEY" if backend == "hunter" else "APOLLO_API_KEY"
            raise ConfigError(
                f"{name} is empty. Add it to .env, or skip API discovery and use "
                "`python main.py import --csv data/targets.csv` with your own contacts."
            )
        return key


# --------------------------------------------------------------------------- .env writer
def update_env_file(updates: Mapping[str, str], path: Path = ENV_FILE) -> None:
    """Set KEY=value lines in .env, keeping every other line. Creates it from the template."""
    if path.exists():
        lines = path.read_text().splitlines()
    elif ENV_TEMPLATE.exists():
        lines = ENV_TEMPLATE.read_text().splitlines()
    else:
        lines = []

    def render(value: str) -> str:
        return f'"{value}"' if re.search(r"[\s#\"']", value) else value

    remaining = dict(updates)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={render(remaining.pop(key))}"
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        for key, value in remaining.items():
            lines.append(f"{key}={render(value)}")

    path.write_text("\n".join(lines) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600: keys live here


def mask(secret: str) -> str:
    """Show just enough of a secret to recognise it."""
    if not secret:
        return "(not set)"
    return f"{secret[:4]}...{secret[-3:]}" if len(secret) > 12 else "set"
