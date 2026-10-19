"""Shared data types and small cleanup helpers: leads, sheet rows, emails, categories, domains."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

from config.settings import CATEGORIES, Col

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

# Role mailboxes that are rarely read by the person we want. Skipped by API discovery.
GENERIC_LOCAL_PARTS = frozenset({
    "info", "support", "help", "hello", "hi", "contact", "contactus", "sales", "admin", "office",
    "team", "press", "media", "pr", "service", "customerservice", "customercare", "orders",
    "billing", "careers", "jobs", "hr", "legal", "privacy", "noreply", "no-reply", "donotreply",
    "webmaster", "abuse", "feedback", "enquiries", "inquiries",
})
# Role mailboxes that ARE the right place to pitch a sponsorship. Kept even though they are roles.
PARTNER_LOCAL_PARTS = frozenset({
    "partnerships", "partnership", "partners", "sponsorship", "sponsorships", "sponsors",
    "athletes", "athlete", "ambassadors", "ambassador", "community", "campus", "collegiate",
    "grassroots", "affiliates", "collabs", "collab",
})

_CATEGORY_ALIASES = {
    "energy": "Energy", "energy drink": "Energy", "energy drinks": "Energy", "caffeine": "Energy",
    "preworkout": "Energy", "pre-workout": "Energy", "pre workout": "Energy",
    "skincare": "Skincare", "skin care": "Skincare", "skin": "Skincare", "sunscreen": "Skincare",
    "spf": "Skincare", "sun care": "Skincare", "beauty": "Skincare",
    "hydration": "Hydration", "electrolytes": "Hydration", "electrolyte": "Hydration",
    "water": "Hydration", "sports drink": "Hydration", "sports drinks": "Hydration",
    "recovery": "Recovery", "tape": "Recovery", "kinesiology tape": "Recovery",
    "massage": "Recovery", "therapy": "Recovery", "compression": "Recovery", "sleep": "Recovery",
    "gear": "Gear", "apparel": "Gear", "equipment": "Gear", "shoes": "Gear", "footwear": "Gear",
    "accessories": "Gear", "athletic wear": "Gear",
}


def normalize_email(raw: str | None) -> str:
    """Lower-cased address, or "" if it is not a plausible email."""
    value = (raw or "").strip().strip("<>\"' ,;")
    if value.lower().startswith("mailto:"):
        value = value[7:]
    value = value.strip().lower()
    return value if _EMAIL_RE.match(value) else ""


def email_local_part(email: str) -> str:
    return email.split("@", 1)[0].lower()


def is_generic_mailbox(email: str) -> bool:
    local = email_local_part(email)
    return local in GENERIC_LOCAL_PARTS and local not in PARTNER_LOCAL_PARTS


def normalize_category(raw: str | None) -> str:
    """Map free text onto Energy / Skincare / Hydration / Recovery / Gear, or "" if unknown."""
    value = (raw or "").strip().lower()
    if not value:
        return ""
    for canonical in CATEGORIES:
        if value == canonical.lower():
            return canonical
    return _CATEGORY_ALIASES.get(value, "")


def normalize_domain(raw: str | None) -> str:
    """'https://www.Example.com/path' -> 'example.com'. Returns "" when nothing usable."""
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if "@" in value and "//" not in value:
        value = value.rsplit("@", 1)[1]
    if "//" not in value:
        value = "//" + value
    host = (urlparse(value).hostname or "").strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host and " " not in host else ""


@dataclass(frozen=True)
class Lead:
    """One contact to add to the sheet (from API discovery or a CSV)."""

    company: str
    email: str = ""
    category: str = ""
    target_name: str = ""
    hook: str = ""
    domain: str = ""
    notes: str = ""

    @property
    def key(self) -> str:
        return self.email.lower()

    def cell_values(self) -> dict[str, str]:
        """Sheet columns this lead fills. Status and Row ID columns are added by SheetManager."""
        return {
            Col.COMPANY: self.company.strip(),
            Col.CATEGORY: self.category,
            Col.TARGET_NAME: self.target_name.strip(),
            Col.TARGET_EMAIL: self.email,
            Col.HOOK: self.hook.strip(),
            Col.NOTES: self.notes.strip(),
            Col.DOMAIN: self.domain,
        }


@dataclass(frozen=True)
class LeadRow:
    """A read-only snapshot of one sheet row. Never write through this: use SheetManager.update_row."""

    values: Mapping[str, str]
    sheet_row: int = 0  # 1-based row number when read; rows move, so identify by row_id

    def get(self, column: str) -> str:
        return str(self.values.get(column) or "").strip()

    @property
    def row_id(self) -> str:
        return self.get(Col.ROW_ID)

    @property
    def company(self) -> str:
        return self.get(Col.COMPANY)

    @property
    def category(self) -> str:
        return self.get(Col.CATEGORY)

    @property
    def target_name(self) -> str:
        return self.get(Col.TARGET_NAME)

    @property
    def email(self) -> str:
        return self.get(Col.TARGET_EMAIL)

    @property
    def hook(self) -> str:
        return self.get(Col.HOOK)

    @property
    def subject(self) -> str:
        return self.get(Col.SUBJECT)

    @property
    def body(self) -> str:
        return self.get(Col.BODY)

    @property
    def approval(self) -> str:
        return self.get(Col.APPROVAL)

    @property
    def send_status(self) -> str:
        return self.get(Col.SEND)

    @property
    def notes(self) -> str:
        return self.get(Col.NOTES)

    @property
    def domain(self) -> str:
        return self.get(Col.DOMAIN)

    def label(self) -> str:
        """Short text for log lines."""
        who = self.target_name or self.email or "(no contact)"
        return f"{self.company or '(no company)'} / {who} [{self.row_id or 'no id'}]"
