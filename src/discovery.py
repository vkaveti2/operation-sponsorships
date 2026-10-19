"""Lead discovery: find who to email at each target brand.

Ways in, all of which end as new `Pending` / `Unsent` rows in the review sheet:
  * a CSV, or another Google Sheet tab, with contacts you already have          -> `import`
  * a CSV or tab with just company + domain, looked up through Hunter or Apollo -> `discover`
  * rows you type straight into the review sheet (nothing to run)

Nothing in this module sends email, and nothing here writes to the sheet. main.py hands the
leads to SheetManager.append_leads, which skips addresses already in the sheet.

Credits cost money, so lookups are capped, previewed with --dry-run, skipped for domains that are
already in the sheet, and stopped as soon as the provider says the plan is used up (leads found so
far are kept).
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import requests
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from config.errors import DiscoveryAuthError, DiscoveryError, QuotaExceeded
from config.google_errors import summarize_exception
from config.settings import TARGET_TITLES, parse_spreadsheet_id
from src.guardrails import find_banned
from src.models import (
    Lead, LeadRow, is_generic_mailbox, normalize_category, normalize_domain, normalize_email,
)

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = (10, 30)
PER_DOMAIN_DEFAULT = 2
MIN_CONFIDENCE_DEFAULT = 50
MIN_TITLE_SCORE = 10
LOOKUP_LIMIT_DEFAULT = 20
HUNTER_RETRY_DELAYS = (2, 6, 15)


# --------------------------------------------------------------------------- targets from a CSV or sheet
@dataclass(frozen=True)
class Target:
    """A brand to find a contact at (or, with an email, a contact we already have)."""

    company: str
    domain: str = ""
    category: str = ""
    hook: str = ""
    email: str = ""
    target_name: str = ""

    @property
    def key(self) -> str:
        return self.domain or self.company.strip().lower()


@dataclass
class ParsedTargets:
    contacts: list[Lead] = field(default_factory=list)  # rows that already have an email: import as is
    lookups: list[Target] = field(default_factory=list)  # rows with a company/domain but no email
    issues: list[tuple[int, str]] = field(default_factory=list)  # (row number in the file, problem)

    @property
    def total(self) -> int:
        return len(self.contacts) + len(self.lookups)


_ALIASES: dict[str, frozenset[str]] = {
    "company": frozenset({"company", "company name", "brand", "brand name", "organization", "organisation", "account"}),
    "domain": frozenset({"domain", "company domain", "website", "site", "url", "web", "company website", "brand website"}),
    "category": frozenset({"category", "type", "industry", "segment", "vertical"}),
    "email": frozenset({"email", "target email", "email address", "contact email", "e-mail", "work email"}),
    "name": frozenset({"target name", "contact", "contact name", "person", "full name"}),
    "hook": frozenset({"custom hook / angle", "custom hook", "hook", "angle", "pitch angle"}),
}


def _header_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _map_header(header: Sequence[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for position, raw in enumerate(header):
        key = _header_key(raw)
        for field_name, names in _ALIASES.items():
            if key in names and field_name not in mapping:
                mapping[field_name] = position
    if "company" not in mapping and _header_key("name") in {_header_key(h) for h in header}:
        mapping["company"] = [_header_key(h) for h in header].index("name")  # a bare "Name" column means the brand
    return mapping


def parse_target_rows(rows: Sequence[Sequence[Any]], *, banned_brands: Iterable[str] = ()) -> ParsedTargets:
    """Turn raw rows (header first) into contacts to import and brands to look up."""
    result = ParsedTargets()
    cleaned = [[str(cell) for cell in row] for row in rows]
    while cleaned and not any(cell.strip() for cell in cleaned[0]):
        cleaned.pop(0)
    if not cleaned:
        raise DiscoveryError("The file is empty. It needs a header row such as: Company Name, Domain, Category.")
    header = _map_header(cleaned[0])
    if "company" not in header:
        raise DiscoveryError(
            "No company column found. Name the first row's columns like: Company Name, Domain, Category "
            "(Target Name, Target Email and Custom Hook / Angle are optional). Found: "
            + ", ".join(h.strip() or "(blank)" for h in cleaned[0])
        )
    if "domain" not in header and "email" not in header:
        raise DiscoveryError("The file needs a Domain column (to look contacts up) or a Target Email column (to import as is).")

    banned = tuple(banned_brands)
    seen: set[str] = set()

    def field_value(row: Sequence[str], name: str) -> str:
        position = header.get(name)
        return row[position].strip() if position is not None and position < len(row) else ""

    for offset, row in enumerate(cleaned[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        company = field_value(row, "company")
        raw_email, raw_domain = field_value(row, "email"), field_value(row, "domain")
        if not company:
            result.issues.append((offset, "no company name, skipped"))
            continue
        if find_banned(company, banned):
            result.issues.append((offset, f"{company} is on BANNED_BRANDS (an existing partner), skipped"))
            continue
        email = normalize_email(raw_email)
        if raw_email and not email:
            result.issues.append((offset, f"'{raw_email}' is not a valid email address, skipped"))
            continue
        domain = normalize_domain(raw_domain) or (normalize_domain(email) if email else "")
        if raw_domain and not normalize_domain(raw_domain):
            result.issues.append((offset, f"'{raw_domain}' is not a valid domain, ignored"))
        category_text = field_value(row, "category")
        category = normalize_category(category_text)
        if category_text and not category:
            result.issues.append((offset, f"unrecognised category '{category_text}', kept as typed (the drafter uses a generic angle)"))
            category = category_text
        elif not category_text:
            result.issues.append((offset, f"{company} has no category, the drafter will use a generic angle"))
        hook, name = field_value(row, "hook"), field_value(row, "name")

        if email:
            if email in seen:
                result.issues.append((offset, f"{email} appears twice in the file, kept once"))
                continue
            seen.add(email)
            result.contacts.append(Lead(company=company, email=email, category=category, target_name=name, hook=hook, domain=domain))
            continue
        target = Target(company=company, domain=domain, category=category, hook=hook)
        if not domain and not company:
            continue
        if target.key in seen:
            result.issues.append((offset, f"{company} appears twice in the file, kept once"))
            continue
        seen.add(target.key)
        result.lookups.append(target)
    return result


def read_targets_csv(path: str | Path, *, banned_brands: Iterable[str] = ()) -> ParsedTargets:
    file = Path(path).expanduser()
    if not file.is_file():
        raise DiscoveryError(f"{path} does not exist. Copy data/targets.example.csv to data/targets.csv and edit it.")
    raw = file.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise DiscoveryError(f"{path} is not a text CSV (could not read its characters). Save it as CSV UTF-8 and try again.")
    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error as exc:
        raise DiscoveryError(f"{path} could not be parsed as CSV ({exc}).") from exc
    return parse_target_rows(rows, banned_brands=banned_brands)


def read_targets_sheet(client: Any, reference: str, tab: str | None = None, *, banned_brands: Iterable[str] = ()) -> ParsedTargets:
    """Read targets from any Google Sheet the signed-in account can open (URL or ID)."""
    sheet_id = parse_spreadsheet_id(reference)
    if not sheet_id:
        raise DiscoveryError("Give the Google Sheet URL or ID to import from.")
    try:
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet(tab) if tab else spreadsheet.sheet1
        values = worksheet.get_all_values()
    except SpreadsheetNotFound as exc:
        raise DiscoveryError("Google could not find that sheet. Check the URL, and that the club Google account can open it (share it with pennstatemcvb@gmail.com).") from exc
    except WorksheetNotFound as exc:
        raise DiscoveryError(f"That sheet has no tab called '{tab}'.") from exc
    except APIError as exc:
        raise DiscoveryError(summarize_exception(exc, "sheets")) from exc
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise DiscoveryError("Could not reach Google Sheets. Check your internet connection.") from exc
    return parse_target_rows(values, banned_brands=banned_brands)


def sheet_domains(rows: Iterable[LeadRow]) -> set[str]:
    """Domains already in the review sheet (from the Company Domain column and from email addresses)."""
    found: set[str] = set()
    for row in rows:
        for candidate in (normalize_domain(row.domain), normalize_domain(row.email)):
            if candidate:
                found.add(candidate)
    return found


# --------------------------------------------------------------------------- ranking contacts by job title
_WORDS = re.compile(r"[a-z]+")
_LEVELS = frozenset({"manager", "specialist", "coordinator", "associate", "lead", "strategist", "representative"})
_LEVEL = "<level>"
_EXCLUDE = frozenset({
    "engineer", "engineering", "engineers", "developer", "software", "finance", "financial", "accounting",
    "accountant", "legal", "counsel", "attorney", "recruiter", "recruiting", "talent", "payroll", "security",
    "procurement", "logistics", "warehouse", "supply", "customer", "support", "nurse", "scientist", "researcher",
})
_DOMAIN_WORDS = frozenset({"marketing", "partnerships", "partnership", "sponsorship", "sponsorships", "communications", "communication", "brand", "pr"})
_CORE_WORDS = frozenset({
    "campus", "collegiate", "college", "university", "student", "students", "field", "grassroots", "sports", "sport",
    "athlete", "athletes", "athletic", "athletics", "partnerships", "partnership", "sponsorship", "sponsorships",
    "community", "ambassador", "ambassadors", "influencer", "experiential", "activation", "events", "event", "amateur",
    "youth", "endorsements", "brand",
})
_TOO_SENIOR = frozenset({"vp", "svp", "evp", "vice", "president", "chief", "cmo", "ceo", "coo", "founder", "cofounder", "owner"})
_TOO_JUNIOR = frozenset({"intern", "interns", "assistant"})


def _normalize_levels(words: Iterable[str]) -> set[str]:
    """Replace Manager / Specialist / Coordinator (and friends) with one shared level marker."""
    return {_LEVEL if word in _LEVELS else word for word in words}


_TARGET_PATTERNS = [
    _normalize_levels(w for w in _WORDS.findall(title.lower()))
    for title in TARGET_TITLES
]


def title_score(position: str) -> int:
    """How good a fit a job title is for a sponsorship pitch. Negative means ignore.

    Any of our five target titles scores highest (Manager, Specialist and Coordinator count as
    the same level, so "Campus Marketing Coordinator" matches "Campus Marketing Manager").
    Otherwise points come from relevant words, and VPs, chiefs and interns are marked down.
    """
    words = set(_WORDS.findall((position or "").lower()))
    if not words or words & _EXCLUDE:
        return -100
    marked = _normalize_levels(words)
    core = len(words & _CORE_WORDS)
    if not (words & _DOMAIN_WORDS) and not core:
        return 0
    score = 0
    if any(pattern <= marked for pattern in _TARGET_PATTERNS):
        score += 60
    score += min(core, 3) * 15
    if words & _DOMAIN_WORDS:
        score += 10
    if words & _LEVELS:
        score += 10
    if words & _TOO_SENIOR:
        score -= 25
    if words & _TOO_JUNIOR:
        score -= 15
    return score


@dataclass(frozen=True)
class Contact:
    email: str
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    confidence: int = 0
    status: str = ""
    source: str = ""
    score: int = 0

    @property
    def full_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        return name.title() if name and (name.isupper() or name.islower()) else name


def rank_contacts(contacts: Iterable[Contact], *, min_confidence: int, keep: int) -> list[Contact]:
    """Best fits first. Drops generic mailboxes, low-confidence addresses, and irrelevant job titles."""
    usable: dict[str, Contact] = {}
    for contact in contacts:
        email = normalize_email(contact.email)
        if not email or is_generic_mailbox(email) or contact.confidence < min_confidence:
            continue
        score = title_score(contact.position)
        if score < MIN_TITLE_SCORE:
            continue
        ranked = Contact(email, contact.first_name.strip(), contact.last_name.strip(), contact.position.strip(),
                         contact.confidence, contact.status, contact.source, score)
        best = usable.get(email)
        if best is None or (ranked.score, ranked.confidence) > (best.score, best.confidence):
            usable[email] = ranked
    ordered = sorted(usable.values(), key=lambda c: (-c.score, -c.confidence, c.email))
    return ordered[:max(1, keep)]


# --------------------------------------------------------------------------- backends
@dataclass
class LookupResult:
    contacts: list[Contact] = field(default_factory=list)
    domain: str = ""
    organization: str = ""
    searches: int = 0  # provider calls that may use a credit
    note: str = ""
    stop: str = ""  # set when the batch must stop (quota or cap). Contacts already found are still kept.


class Backend(Protocol):
    name: str

    def find_contacts(self, target: Target, *, per_domain: int, broad: bool, min_confidence: int) -> LookupResult: ...


def _error_text(response: Any) -> str:
    try:
        data = response.json()
    except ValueError:
        return (getattr(response, "text", "") or "")[:200]
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("details") or errors[0].get("id") or errors[0])[:200]
        detail = data.get("error_details")
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("code") or detail)[:200]
        return str(data.get("message") or data.get("error") or data)[:200]
    return str(data)[:200]


class HunterBackend:
    """Hunter.io Domain Search: one GET per brand. A search uses a credit only when it returns results."""

    name = "Hunter"
    URL = "https://api.hunter.io/v2/domain-search"
    DEPARTMENTS = "marketing,communication"
    LIMIT = 10  # the free plan caps limit+offset at 10, and 10 is enough once titles are filtered

    def __init__(self, api_key: str, *, session: Any = None, sleep: Callable[[float], None] = time.sleep):
        self._key = api_key
        self._session = session or requests.Session()
        self._sleep = sleep

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"X-API-KEY": self._key, "Accept": "application/json"}  # in a header, never in the URL
        attempts = len(HUNTER_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                response = self._session.get(self.URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as exc:
                if not last:
                    self._sleep(HUNTER_RETRY_DELAYS[attempt])
                    continue
                raise DiscoveryError("Could not reach Hunter. Check your internet connection.") from exc
            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise DiscoveryError("Hunter sent a reply that could not be read.") from exc
                return data if isinstance(data, dict) else {}
            if status == 401:
                raise DiscoveryAuthError("Hunter rejected HUNTER_API_KEY. Copy the key again from https://hunter.io/api-keys")
            if status == 429:
                raise QuotaExceeded("Your Hunter plan has no searches left this period. Leads found so far are kept. Upgrade, wait for the reset, or use `import --csv` with your own contacts.")
            if status in (403, 500, 502, 503, 504) and not last:  # 403 here means "too many requests per second"
                self._sleep(HUNTER_RETRY_DELAYS[attempt])
                continue
            if status == 403:
                raise DiscoveryError("Hunter is rate limiting requests. Wait a minute and run it again.")
            if status >= 500:
                raise DiscoveryError(f"Hunter had a server problem ({status}). Try again later.")
            raise DiscoveryError(f"Hunter rejected the search ({status}): {_error_text(response)}")
        raise DiscoveryError("Hunter did not respond.")

    @staticmethod
    def _contacts(data: Mapping[str, Any]) -> list[Contact]:
        out: list[Contact] = []
        for entry in (data.get("data") or {}).get("emails") or []:
            if not isinstance(entry, dict) or entry.get("type") == "generic":
                continue
            verification = entry.get("verification") or {}
            status = str(verification.get("status") or "") if isinstance(verification, dict) else ""
            if status == "invalid":
                continue
            try:
                confidence = int(entry.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0
            out.append(Contact(
                email=str(entry.get("value") or ""), first_name=str(entry.get("first_name") or ""),
                last_name=str(entry.get("last_name") or ""), position=str(entry.get("position") or ""),
                confidence=confidence, status=status, source=HunterBackend.name,
            ))
        return out

    def find_contacts(self, target: Target, *, per_domain: int = PER_DOMAIN_DEFAULT, broad: bool = True,
                      min_confidence: int = MIN_CONFIDENCE_DEFAULT) -> LookupResult:
        base: dict[str, Any] = {"limit": self.LIMIT, "type": "personal", "department": self.DEPARTMENTS}
        if target.domain:
            base["domain"] = target.domain
        else:
            base["company"] = target.company  # Hunter resolves the company name itself
        passes = [dict(base, job_titles=",".join(TARGET_TITLES))]
        if broad:
            passes.append(dict(base))
        result = LookupResult()
        for number, params in enumerate(passes, start=1):
            data = self._get(params)
            result.searches += 1
            info = data.get("data") or {}
            result.domain = result.domain or normalize_domain(str(info.get("domain") or ""))
            result.organization = result.organization or str(info.get("organization") or "")
            ranked = rank_contacts(self._contacts(data), min_confidence=min_confidence, keep=per_domain)
            if ranked:
                result.contacts = ranked
                result.note = "matched a target job title" if number == 1 else "closest marketing contacts (no exact title match)"
                return result
            self._sleep(0.25)
        result.note = "no usable contacts found"
        return result


class ApolloBackend:
    """Apollo.io: People Search (free, no emails) then People Enrichment for the best few (1 credit each)."""

    name = "Apollo"
    SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
    MATCH_URL = "https://api.apollo.io/api/v1/people/match"
    SENIORITIES = ("manager", "senior", "entry", "director", "head")
    CONFIDENCE = {"verified": 90, "extrapolated": 60}
    BAD_STATUS = frozenset({"unavailable", "invalid", "bounced", "unverified_invalid"})

    def __init__(self, api_key: str, *, session: Any = None, sleep: Callable[[float], None] = time.sleep,
                 reveal_cap: int | None = None):
        self._key = api_key
        self._session = session or requests.Session()
        self._sleep = sleep
        self.reveal_cap = reveal_cap
        self.reveals = 0

    def _post(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-api-key": self._key, "Accept": "application/json", "Cache-Control": "no-cache"}
        try:
            response = self._session.post(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise DiscoveryError("Could not reach Apollo (the request may or may not have used a credit). Try again.") from exc
        status = response.status_code
        if 200 <= status < 300:
            try:
                data = response.json()
            except ValueError as exc:
                raise DiscoveryError("Apollo sent a reply that could not be read.") from exc
            return data if isinstance(data, dict) else {}
        if status == 401:
            raise DiscoveryAuthError("Apollo rejected APOLLO_API_KEY. Create a new key at https://app.apollo.io/#/settings/integrations/api")
        if status == 403:
            raise DiscoveryAuthError("This Apollo key cannot use that endpoint. Create a master API key (Settings, Integrations, API) or enable the route for the key.")
        if status == 429:
            raise QuotaExceeded("Apollo is rate limiting this key or the plan is out of credits. Leads found so far are kept. Try again later.")
        if status >= 500:
            raise DiscoveryError(f"Apollo had a server problem ({status}). Try again later.")
        raise DiscoveryError(f"Apollo rejected the request ({status}): {_error_text(response)}")

    def find_contacts(self, target: Target, *, per_domain: int = PER_DOMAIN_DEFAULT, broad: bool = True,
                      min_confidence: int = MIN_CONFIDENCE_DEFAULT) -> LookupResult:
        # `broad` is unused: Apollo's similar-title matching already widens the search.
        if not target.domain:
            raise DiscoveryError(f"{target.company}: Apollo lookups need a Domain (for example redbull.com).")
        data = self._post(self.SEARCH_URL, {
            "person_titles[]": list(TARGET_TITLES), "include_similar_titles": "true",
            "q_organization_domains_list[]": [target.domain], "person_seniorities[]": list(self.SENIORITIES),
            "per_page": 10, "page": 1,
        })
        result = LookupResult(domain=target.domain)
        candidates = []
        for person in data.get("people") or []:
            if not isinstance(person, dict) or not person.get("id") or not person.get("has_email"):
                continue
            score = title_score(str(person.get("title") or ""))
            if score >= MIN_TITLE_SCORE:
                candidates.append((score, person))
        candidates.sort(key=lambda item: -item[0])
        for _, person in candidates[:max(1, per_domain)]:
            try:
                if self.reveal_cap is not None and self.reveals >= self.reveal_cap:
                    raise QuotaExceeded(f"Reached the limit of {self.reveal_cap} Apollo email lookups for this run (--max-reveals). Leads found so far are kept.")
                self.reveals += 1
                result.searches += 1
                reply = self._post(self.MATCH_URL, {"id": person["id"], "reveal_personal_emails": "false", "reveal_phone_number": "false"})
            except QuotaExceeded as exc:
                result.stop = str(exc)  # credits already spent on earlier people in this brand must not be thrown away
                break
            matched = reply.get("person") or {}
            email = normalize_email(str(matched.get("email") or ""))
            status = str(matched.get("email_status") or "").lower()
            if not email or email.startswith("email_not_unlocked") or status in self.BAD_STATUS:
                continue
            organization = matched.get("organization") or {}
            if isinstance(organization, dict):
                result.organization = result.organization or str(organization.get("name") or "")
            result.contacts.append(Contact(
                email=email, first_name=str(matched.get("first_name") or person.get("first_name") or ""),
                last_name=str(matched.get("last_name") or ""), position=str(matched.get("title") or person.get("title") or ""),
                confidence=self.CONFIDENCE.get(status, 50), status=status, source=self.name,
            ))
            self._sleep(0.3)
        result.contacts = rank_contacts(result.contacts, min_confidence=min_confidence, keep=per_domain)
        result.note = "matched a target job title" if result.contacts else "no usable contacts found"
        return result


def build_backend(name: str, api_key: str, *, session: Any = None, reveal_cap: int | None = None,
                  sleep: Callable[[float], None] = time.sleep) -> Backend:
    if name == "apollo":
        return ApolloBackend(api_key, session=session, sleep=sleep, reveal_cap=reveal_cap)
    return HunterBackend(api_key, session=session, sleep=sleep)


# --------------------------------------------------------------------------- running a discovery batch
@dataclass
class DiscoveryReport:
    leads: list[Lead] = field(default_factory=list)
    searched: int = 0  # brands looked up
    searches: int = 0  # provider calls that may have used a credit
    without_contacts: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    remaining: int = 0
    aborted: str = ""


def plan_lookups(targets: Sequence[Target], *, existing_domains: Iterable[str] = (), banned_brands: Iterable[str] = (),
                 limit: int | None = LOOKUP_LIMIT_DEFAULT) -> tuple[list[Target], list[tuple[str, str]], int]:
    """(brands that would be looked up, brands skipped with the reason, brands left because of the limit)."""
    have = set(existing_domains)
    banned = tuple(banned_brands)
    todo: list[Target] = []
    skipped: list[tuple[str, str]] = []
    left = 0
    for target in targets:
        if find_banned(target.company, banned):
            skipped.append((target.company, "existing partner (BANNED_BRANDS)"))
        elif target.domain and target.domain in have:
            skipped.append((target.company, f"{target.domain} is already in the sheet"))
        elif limit is not None and len(todo) >= limit:
            left += 1
        else:
            todo.append(target)
    return todo, skipped, left


def discover(
    targets: Sequence[Target],
    backend: Backend,
    *,
    existing_domains: Iterable[str] = (),
    banned_brands: Iterable[str] = (),
    per_domain: int = PER_DOMAIN_DEFAULT,
    broad: bool = True,
    min_confidence: int = MIN_CONFIDENCE_DEFAULT,
    limit: int | None = LOOKUP_LIMIT_DEFAULT,
) -> DiscoveryReport:
    todo, skipped, left = plan_lookups(targets, existing_domains=existing_domains, banned_brands=banned_brands, limit=limit)
    report = DiscoveryReport(skipped=skipped, remaining=left)
    found_emails: set[str] = set()
    for index, target in enumerate(todo):
        try:
            result = backend.find_contacts(target, per_domain=per_domain, broad=broad, min_confidence=min_confidence)
        except (DiscoveryAuthError, QuotaExceeded) as exc:
            report.aborted = str(exc)
            report.remaining += len(todo) - index
            break
        except DiscoveryError as exc:
            report.errors.append((target.company, str(exc)))
            report.searched += 1
            continue
        report.searched += 1
        report.searches += result.searches
        fresh = [c for c in result.contacts if c.email not in found_emails]
        if result.stop:
            report.aborted = result.stop
            report.remaining += len(todo) - index - (1 if fresh else 0)
        elif not fresh:
            report.without_contacts.append(target.company)
            continue
        for contact in fresh:
            found_emails.add(contact.email)
            detail = f"{backend.name}: {contact.position or 'title unknown'}, confidence {contact.confidence}"
            if contact.status:
                detail += f" ({contact.status})"
            report.leads.append(Lead(
                company=target.company, email=contact.email, category=target.category,
                target_name=contact.full_name, hook=target.hook,
                domain=result.domain or target.domain, notes=f"[found] {detail}",
            ))
        if result.stop:
            break
    return report
