"""Command-line interface. main.py is a thin wrapper around main() here.

    python main.py configure      save your model API key (and optional keys) to .env
    python main.py auth           sign in to Google as the club account (one login for Sheets + Gmail)
    python main.py init-sheet     create the review sheet
    python main.py import         add contacts you already have (CSV or another Google Sheet)
    python main.py discover       find contacts at brands with Hunter or Apollo, then add them
    python main.py try-draft      write one sample email with the real model (no Google login needed)
    python main.py draft          write emails for Pending rows
    python main.py send           create Gmail drafts for Approved rows  (--live to really send)
    python main.py sync           mark rows Sent once you have sent their drafts by hand
    python main.py status         where every row stands, and what to do next
    python main.py doctor         check the setup and say exactly what is missing

Commands print what they did in plain words. Expected problems end with "Error: <how to fix it>"
and exit code 1. Ctrl+C exits with 130.
"""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import importlib
import logging
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from config.errors import ConfigError, OutreachError
from config.logging_setup import setup_logging
from config.settings import (
    CREDENTIALS_FILE, DATA_DIR, ENV_FILE, LOGS_DIR, MAX_DELAY_SECONDS, MIN_DELAY_SECONDS, PROJECT_ROOT, TOKEN_FILE,
    Approval, Col, SendStatus, Settings, mask, season_context, update_env_file,
)
from src import discovery as disc
from src.drafter import DEFAULT_LIMIT, Drafter, build_system_prompt, build_user_prompt, pick_angle
from src.llm import LLMClient, ManualLLM, build_llm, build_manual_llm
from src.mailer import GmailApi, Mailer, count_sent_since, local_now
from src.models import LeadRow, normalize_category
from src.sheet_manager import DEFAULT_TITLE, SheetManager, same

log = logging.getLogger(__name__)

DEFAULT_TARGETS_CSV = DATA_DIR / "targets.csv"
EXAMPLE_TARGETS_CSV = DATA_DIR / "targets.example.csv"
MIN_PYTHON = (3, 10)
REQUIRED_MODULES = {
    "anthropic": "anthropic", "google.genai": "google-genai", "gspread": "gspread", "google.auth": "google-auth",
    "google_auth_oauthlib": "google-auth-oauthlib", "googleapiclient": "google-api-python-client",
    "dotenv": "python-dotenv", "requests": "requests",
}
KEY_HINTS = {
    "anthropic": ("Claude", "ANTHROPIC_API_KEY", ("sk-ant-",), "https://platform.claude.com/settings/keys"),
    "gemini": ("Gemini", "GEMINI_API_KEY", ("AIza", "AQ."), "https://aistudio.google.com/apikey"),
}


# --------------------------------------------------------------------------- runtime
class Runtime:
    """Everything a command needs, with real implementations by default. Tests pass fakes in."""

    def __init__(
        self,
        settings: Settings,
        *,
        out: Callable[[str], None] | None = None,
        ask: Callable[[str], str] = input,
        ask_secret: Callable[[str], str] = getpass.getpass,
        read_stdin: Callable[[], str] = lambda: sys.stdin.read(),
        interactive: bool | None = None,
        today: date | None = None,
        env_path: Path = ENV_FILE,
        lookups_path: Path = DATA_DIR / "lookups.json",
        creds: Any = None,
        gspread_client: Any = None,
        sheet: SheetManager | None = None,
        gmail: Any = None,
        llm: LLMClient | None = None,
        backend: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = local_now,
    ):
        self.settings = settings
        self._out = out or (lambda text: print(text, flush=True))
        self.ask, self.ask_secret, self.read_stdin = ask, ask_secret, read_stdin
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.today = today
        self.env_path = env_path
        self.lookups_path = lookups_path
        self.sleep, self.clock = sleep, clock
        self._creds, self._gspread, self._sheet, self._gmail, self._llm, self._backend = creds, gspread_client, sheet, gmail, llm, backend

    def say(self, text: str = "") -> None:
        self._out(text)

    # ---- lazily built collaborators
    def creds(self) -> Any:
        if self._creds is None:
            from config.credentials import load_credentials
            self._creds = load_credentials()
        return self._creds

    def gspread(self) -> Any:
        if self._gspread is None:
            from config.credentials import get_gspread_client
            self._gspread = get_gspread_client(self.creds())
        return self._gspread

    def sheet(self) -> SheetManager:
        if self._sheet is None:
            self._sheet = SheetManager.connect(self.settings, self.gspread(), sleep=self.sleep)
        return self._sheet

    def gmail(self) -> Any:
        if self._gmail is None:
            from google.auth.transport.requests import AuthorizedSession
            self._gmail = GmailApi(AuthorizedSession(self.creds()))
        return self._gmail

    def llm(self, *, manual: bool = False) -> LLMClient:
        if manual:
            return build_manual_llm()
        if self._llm is None:
            self._llm = build_llm(self.settings)
        return self._llm

    def backend(self, name: str, *, reveal_cap: int | None = None) -> Any:
        if self._backend is None:
            self._backend = disc.build_backend(name, self.settings.discovery_key(name), reveal_cap=reveal_cap, sleep=self.sleep)
        return self._backend

    def hunter_ready(self) -> bool:
        """True when a Hunter call can be made: a key is configured, or a backend was supplied (tests)."""
        return bool(self.settings.hunter_api_key) or self._backend is not None


# --------------------------------------------------------------------------- small helpers
def rel(path: Path) -> str:
    """Path shown relative to the project when it is inside it, else as given."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def clip(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: max(1, width - 3)] + "..."


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {many or word + 's'}"


def sheet_hint(rt: Runtime) -> str:
    return f"Open the sheet: {rt.sheet().url}"


def finish_code(*, aborted: str = "", problem: bool = False) -> int:
    return 1 if (aborted or problem) else 0


# --------------------------------------------------------------------------- configure
def _looks_right(provider: str, key: str) -> bool:
    return key.startswith(KEY_HINTS[provider][2])


def cmd_configure(rt: Runtime, args: argparse.Namespace) -> int:
    s = rt.settings
    updates: dict[str, str] = {}
    provider = (args.provider or "").lower()
    if not provider:
        provider = s.llm_provider
        if rt.interactive:
            provider = (rt.ask(f"Which model writes the drafts? anthropic (Claude) or gemini [{s.llm_provider}]: ").strip().lower() or s.llm_provider)
    if provider not in KEY_HINTS:
        raise ConfigError("The provider must be 'anthropic' or 'gemini'.")
    label, env_name, _, url = KEY_HINTS[provider]
    updates["LLM_PROVIDER"] = provider

    key = ""
    if args.key_from_stdin:
        key = rt.read_stdin().strip().strip("\"'")
        if not key:
            raise ConfigError("Nothing was piped in. Example: pbpaste | python main.py configure --key-from-stdin")
    elif rt.interactive:
        have = mask(s.anthropic_api_key if provider == "anthropic" else s.gemini_api_key)
        key = rt.ask_secret(f"Paste your {label} API key from {url}\n(typing is hidden; Enter keeps the current one: {have}): ").strip().strip("\"'")
    if key:
        if not _looks_right(provider, key):
            prefixes = " or ".join(KEY_HINTS[provider][2])
            rt.say(f"Note: {label} keys normally start with {prefixes}. Saving it anyway; `doctor --online` will tell you if it works.")
        updates[env_name] = key

    if rt.interactive and not args.key_from_stdin:
        for name, text in (("HUNTER_API_KEY", "Hunter.io API key for finding contacts (optional, Enter to skip): "),
                           ("APOLLO_API_KEY", "Apollo.io API key (optional, Enter to skip): ")):
            value = rt.ask_secret(text).strip().strip("\"'")
            if value:
                updates[name] = value
        email = rt.ask("Your own Google account email, so you can open the review sheet (optional, Enter to skip): ").strip()
        if email:
            updates["SHARE_SHEET_WITH"] = email

    update_env_file(updates, path=rt.env_path)
    rt.say(f"Saved to {rt.env_path.name}: " + ", ".join(f"{k}={mask(v) if 'KEY' in k else v}" for k, v in updates.items()))
    if provider == "gemini":
        rt.say("Privacy note: on Gemini's free tier Google may use prompts to improve its products (a paid-tier key does not). "
               "The model is only ever sent company names, categories and your optional notes, never contact names or emails. "
               "See exactly what with: python main.py try-draft --show-prompt")

    key_ok = False
    if key and not args.no_check:
        fresh = dataclasses.replace(
            s, llm_provider=provider,
            **{("anthropic_api_key" if provider == "anthropic" else "gemini_api_key"): key},
        )
        try:
            rt.say(f"Checking the key... {build_llm(fresh).check()}.")
            key_ok = True
        except OutreachError as exc:
            rt.say(f"Saved, but the check failed: {exc}")
            return 1
    if key_ok:
        rt.say("Next: python main.py try-draft   (writes one real sample email; no Google login needed)")
    else:
        rt.say("Next: python main.py auth" if not TOKEN_FILE.exists() else "Next: python main.py doctor --online")
    return 0


# --------------------------------------------------------------------------- auth / init-sheet
def cmd_auth(rt: Runtime, args: argparse.Namespace) -> int:
    from config.credentials import run_auth_flow
    run_auth_flow(rt.settings.sender.email, open_browser=not args.no_browser)
    rt.say(f"Signed in as {rt.settings.sender.email}. The login is saved in config/token.json (private to you, never committed).")
    rt.say("Next: python main.py init-sheet" if not rt.settings.spreadsheet_id else "Next: python main.py doctor --online")
    return 0


def cmd_init_sheet(rt: Runtime, args: argparse.Namespace) -> int:
    s = rt.settings
    if args.repair:
        sheet = rt.sheet()
        added = sheet.ensure_layout()
        rt.say(("Added missing columns: " + ", ".join(added)) if added else "All columns were already there.")
        rt.say("Dropdowns, colors and column widths are up to date.")
        rt.say(f"Sheet: {sheet.url}")
        return 0
    if s.spreadsheet_id and not args.new:
        raise OutreachError(
            f"A sheet is already set up (SPREADSHEET_ID in .env). Use --repair to fix its layout, or --new to create a second one.\n"
            f"Existing: https://docs.google.com/spreadsheets/d/{s.spreadsheet_id}"
        )
    share = list(dict.fromkeys([*s.share_sheet_with, *(args.share or [])]))
    sheet = SheetManager.create_new(s, rt.gspread(), title=args.title, share_with=share, sleep=rt.sleep)
    update_env_file({"SPREADSHEET_ID": sheet.spreadsheet_id}, path=rt.env_path)
    rt.say(f"Created '{sheet.title}' and saved its ID to .env.")
    rt.say(f"Open it: {sheet.url}")
    rt.say("It belongs to " + s.sender.email + (f" and is shared with {', '.join(share)}." if share else ". To see it from your own Google account, run: python main.py init-sheet --new --share you@gmail.com (or open the link while signed in as the club account)."))
    rt.say("Next: python main.py import --csv data/targets.csv   (or: discover)   then: python main.py draft")
    return 0


# --------------------------------------------------------------------------- import / discover
def load_targets(rt: Runtime, args: argparse.Namespace) -> disc.ParsedTargets:
    banned = rt.settings.banned_brands
    if getattr(args, "sheet", None):
        return disc.read_targets_sheet(rt.gspread(), args.sheet, getattr(args, "tab", None), banned_brands=banned)
    path = Path(args.csv) if getattr(args, "csv", None) else DEFAULT_TARGETS_CSV
    if not path.exists() and not getattr(args, "csv", None):
        raise OutreachError(
            f"No brand list found. Copy the example and edit it:\n  cp {rel(EXAMPLE_TARGETS_CSV)} {rel(DEFAULT_TARGETS_CSV)}\n"
            "or point at another file with --csv PATH or a Google Sheet with --sheet URL."
        )
    return disc.read_targets_csv(path, banned_brands=banned)


def _report_issues(rt: Runtime, parsed: disc.ParsedTargets) -> None:
    for row, message in parsed.issues:
        rt.say(f"  row {row}: {message}")


def _add_to_sheet(rt: Runtime, leads: Sequence[Any]) -> tuple[int, int]:
    sheet = rt.sheet()
    result = sheet.append_leads(leads)
    for lead, reason in result.invalid:
        rt.say(f"  not added: {lead.company or '(no company)'} ({reason})")
    for lead in result.duplicates:
        rt.say(f"  already in the sheet: {lead.email}")
    return len(result.added_ids), len(result.duplicates)


def cmd_import(rt: Runtime, args: argparse.Namespace) -> int:
    parsed = load_targets(rt, args)
    _report_issues(rt, parsed)
    added, dupes = _add_to_sheet(rt, parsed.contacts) if parsed.contacts else (0, 0)
    rt.say(f"Added {plural(added, 'row')} to the sheet" + (f", skipped {plural(dupes, 'duplicate')}." if dupes else "."))
    if parsed.lookups:
        rt.say(f"{plural(len(parsed.lookups), 'brand')} in that file have no email address, so they were not added. Either:")
        rt.say("  - fill in an email column and import again, or")
        rt.say("  - let Hunter or Apollo find contacts for them: python main.py discover (needs an API key, see README)")
    if added:
        rt.say(sheet_hint(rt))
        rt.say("Next: python main.py draft")
    return 0


def _show_hunter_balance(rt: Runtime, worst_case: int) -> disc.Balance | None:
    """Print the real Hunter credit balance (a free call). Never fails the command: None when it cannot be read."""
    if not rt.hunter_ready():
        return None
    try:
        fetch = getattr(rt.backend("hunter"), "balance", None)
        balance = fetch() if fetch else None
    except OutreachError as exc:
        rt.say(f"(Could not read your Hunter balance: {exc})")
        return None
    if balance is None:
        return None
    rt.say(f"Your Hunter plan ({balance.plan or 'unknown'}): {balance.remaining:g} of {balance.available:g} credits left"
           + (f", resetting {balance.reset_date}." if balance.reset_date else "."))
    if balance.remaining < worst_case:
        rt.say(f"That is fewer than the {worst_case} this run could use, so it stops when the credits run out and keeps what it found.")
    return balance


def cmd_discover(rt: Runtime, args: argparse.Namespace) -> int:
    s = rt.settings
    backend_name = (args.backend or s.discovery_backend).lower()
    parsed = load_targets(rt, args)
    _report_issues(rt, parsed)

    existing: set[str] = set()
    try:
        existing = disc.sheet_domains(rt.sheet().read_rows())
    except OutreachError as exc:
        if not args.dry_run:
            raise
        rt.say(f"(Could not read the review sheet to skip brands already in it: {exc})")
    memory = disc.LookupLog(rt.lookups_path)
    if memory.damaged:
        rt.say(f"(Could not read {rel(rt.lookups_path)}, so every brand counts as not searched yet.)")
    today = rt.today or date.today()
    remembered = {t.key: why for t in parsed.lookups if (why := memory.skip_reason(t.key, today, args.recheck_days))}
    todo, skipped, left = disc.plan_lookups(parsed.lookups, existing_domains=existing, banned_brands=s.banned_brands, limit=args.limit,
                                            remembered=remembered)
    for company, reason in skipped:
        rt.say(f"  skipping {company}: {reason}")
    rt.say(f"{plural(len(todo), 'brand')} to look up with {backend_name.title()}" + (f", {left} more waiting (raise --limit to include them)" if left else "")
           + (f"; {plural(len(parsed.contacts), 'contact')} already have an email and are added directly." if parsed.contacts else "."))
    out_of_credits = False
    if backend_name == "apollo":
        rt.say(f"Apollo uses about 1 credit per email revealed (up to {args.per_domain} per brand, capped at {args.max_reveals} this run).")
    else:
        worst = len(todo) * (1 if args.no_broad else 2)
        rt.say("Hunter charges 1 credit for each search that returns results (1 to 10 emails). "
               + ("--no-broad is on, so each brand takes one search" if args.no_broad else "A brand can take a second, wider search when no exact title matches")
               + f", so budget up to {worst} credits for this run. Hunter's free plan includes 50 credits a month"
               + ("." if args.no_broad else "; add --no-broad to cap it at one credit per brand."))
        balance = _show_hunter_balance(rt, worst)
        if balance is not None and balance.remaining < 1 and todo and not args.dry_run:
            rt.say("No Hunter credits left, so no brands were looked up" + (f" (they reset on {balance.reset_date})" if balance.reset_date else "")
                   + ". `import --csv` still works with contacts you already have.")
            todo, out_of_credits = [], True
    if args.dry_run:
        for target in todo:
            rt.say(f"  would look up: {target.company} ({target.domain or 'domain unknown, by name'}) [{target.category or 'no category'}]")
        rt.say("Dry run: nothing was looked up or added.")
        return 0

    added_direct = dupes_direct = 0
    if parsed.contacts:
        added_direct, dupes_direct = _add_to_sheet(rt, parsed.contacts)
    report = disc.DiscoveryReport()
    if todo:
        backend = rt.backend(backend_name, reveal_cap=args.max_reveals if backend_name == "apollo" else None)
        report = disc.discover(todo, backend, existing_domains=existing, banned_brands=s.banned_brands, per_domain=args.per_domain,
                               broad=not args.no_broad, min_confidence=args.min_confidence, limit=None)
        names = {t.key: t.company for t in todo}
        for key in report.dead_end_keys:
            memory.record(key, names.get(key, key), today)
        for key in report.found_keys:
            memory.forget(key)
        try:
            memory.save()
        except OSError as exc:
            rt.say(f"(Could not save {rel(rt.lookups_path)}: {exc}. Brands with no result will be searched again next time.)")
        for company, message in report.errors:
            rt.say(f"  {company}: {message}")
        if report.without_contacts:
            rt.say("  no usable contact found for: " + ", ".join(report.without_contacts)
                   + (f" (not searched again for {args.recheck_days} days)" if args.recheck_days > 0 else ""))
        for lead in report.leads:
            rt.say(f"  found: {lead.company}: {lead.target_name or '(no name)'} <{lead.email}>  {clip(lead.notes.replace('[found] ', ''), 70)}")
    added, dupes = _add_to_sheet(rt, report.leads) if report.leads else (0, 0)
    total_added = added + added_direct
    rt.say(f"Added {plural(total_added, 'row')} to the sheet" + (f", skipped {plural(dupes + dupes_direct, 'duplicate')}." if dupes + dupes_direct else "."))
    if report.aborted:
        rt.say(f"Stopped early: {report.aborted}")
        if report.remaining:
            rt.say(f"{plural(report.remaining, 'brand')} were not looked up. Run the same command later to continue; brands already in the sheet are skipped.")
    if total_added:
        rt.say(sheet_hint(rt))
        rt.say("Next: python main.py draft")
    return finish_code(aborted=report.aborted, problem=out_of_credits)


# --------------------------------------------------------------------------- draft
def cmd_draft(rt: Runtime, args: argparse.Namespace) -> int:
    manual = getattr(args, "manual", False)
    drafter = Drafter(rt.settings, rt.sheet(), rt.llm(manual=manual), today=rt.today)
    report = drafter.run(limit=args.limit, row_ids=args.row or (), dry_run=args.dry_run)
    for outcome in report.previews:
        rt.say(f"\n--- preview for {outcome.label} ---\nSubject: {outcome.subject}\n\n{outcome.email}\n")
    for outcome in report.drafted:
        rt.say(f"  drafted  {clip(outcome.label, 48):48s} {clip(outcome.subject, 56)}")
    for outcome in report.failed:
        rt.say(f"  FAILED   {clip(outcome.label, 48):48s} {clip(outcome.reason, 100)}")
    for outcome in report.skipped:
        rt.say(f"  skipped  {clip(outcome.label, 48):48s} {clip(outcome.reason, 100)}")
    verb = "Previewed" if args.dry_run else "Drafted"
    count = len(report.previews) if args.dry_run else len(report.drafted)
    rt.say(f"{verb} {plural(count, 'email')}; {len(report.failed)} failed; {len(report.skipped)} skipped. "
           f"Model usage: {report.input_tokens:,} tokens in, {report.output_tokens:,} out.")
    if report.remaining:
        rt.say(f"{plural(report.remaining, 'more Pending row')} not drafted because of the limit of {args.limit}. Run draft again for the next batch.")
    if report.aborted:
        rt.say(f"Stopped early: {report.aborted}")
    if report.drafted:
        rt.say(sheet_hint(rt))
        rt.say("Review each email, edit anything you like in the cell, then set Approval Status to Approved (or Rejected). Then: python main.py send")
    if report.failed and not report.aborted:
        rt.say("Failed rows stay Pending with the reason in the Notes column. Fix the row (or its Custom Hook / Angle) and run draft again.")
    return finish_code(aborted=report.aborted)


def _next_step_after_model_works(rt: Runtime) -> str:
    if not TOKEN_FILE.exists():
        return "Next: python main.py auth   (sign in to Google as the club account)"
    if not rt.settings.spreadsheet_id:
        return "Next: python main.py init-sheet"
    return "Next: python main.py status"


def cmd_try_draft(rt: Runtime, args: argparse.Namespace) -> int:
    """One sample email with the real model. Needs only the model key: no Google login, no sheet, nothing saved."""
    s = rt.settings
    manual = getattr(args, "manual", False)
    company = (args.company or "").strip()
    category = normalize_category(args.category) or (args.category or "").strip()
    row = LeadRow({
        Col.ROW_ID: "PREVIEW", Col.COMPANY: company, Col.CATEGORY: category, Col.TARGET_NAME: (args.name or "").strip(),
        Col.TARGET_EMAIL: "preview@example.com", Col.HOOK: (args.hook or "").strip(),
    })
    llm = rt.llm(manual=manual)
    if manual:
        rt.say(f"Manual mode: prompts will be copied to your clipboard for {company or '(no company)'} ({category or 'no category'}).")
        rt.say("Nothing is saved or sent, and no Google login is needed.")
    else:
        rt.say(f"Writing a sample email for {company or '(no company)'} ({category or 'no category'}) with {llm.provider} ({llm.model}).")
        rt.say("Nothing is saved or sent, and no Google login is needed.")
    if args.show_prompt:
        season = season_context(rt.today or date.today())
        rt.say("\n--- exactly what the model is sent ---\nSYSTEM:\n" + build_system_prompt(s.sender, season)
               + "\n\nUSER:\n" + build_user_prompt(row, pick_angle(row), season) + "\n--------------------------------------")
    outcome, report = Drafter(s, None, llm, today=rt.today).preview(row)
    usage = f"Model usage: {report.input_tokens:,} tokens in, {report.output_tokens:,} out."

    if outcome.status == "preview":
        tries = "1 try" if outcome.attempts == 1 else f"{outcome.attempts} tries"
        rt.say(f"\nSubject: {outcome.subject}\n\n{outcome.email}\n")
        rt.say(f"Passed every rule check ({tries}). {usage}")
        rt.say("`python main.py draft` writes emails like this into the sheet for you to approve.")
        rt.say(_next_step_after_model_works(rt))
        return 0
    if outcome.status == "skipped":
        rt.say(f"\nNothing was written: {outcome.reason}")
        return 1
    if outcome.problems:
        rt.say(f"\nThe model's draft broke these rules after {outcome.attempts} tries, so `draft` would have rejected it:")
        for problem in outcome.problems:
            rt.say(f"  - {problem}")
        rt.say("\nIts last attempt, for reference:\n" + (outcome.raw.strip() or "(empty)") + "\n")
        rt.say(usage)
        rt.say("In the real flow the row stays Pending with these reasons in Notes. If this happens every time, the prompt in src/drafter.py needs tuning for this model.")
        return 1
    rt.say(f"\nThe model did not produce a draft: {outcome.reason}")
    rt.say(usage)
    return 1


# --------------------------------------------------------------------------- send / sync
def _print_plan(rt: Runtime, ready: Sequence[LeadRow], blocked: Sequence[tuple[LeadRow, list[str]]], remaining: int) -> None:
    for row in ready:
        rt.say(f"  {clip(row.company, 24):24s} {clip(row.email, 34):34s} {clip(row.subject, 52)}")
    for row, problems in blocked:
        rt.say(f"  BLOCKED  {clip(row.label(), 44)}: {'; '.join(problems)}")
    if remaining:
        rt.say(f"  ({plural(remaining, 'more approved row')} wait for the next run: the cap is {rt.settings.max_per_run} per run.)")


def cmd_send(rt: Runtime, args: argparse.Namespace) -> int:
    mailer = Mailer(rt.settings, rt.sheet(), rt.gmail(), sleep=rt.sleep, clock=rt.clock)
    mode = "LIVE: emails will really be sent" if args.live else "drafts: nothing is sent, you press Send in Gmail"
    if args.preview:
        ready, blocked, remaining = mailer.plan(args.limit)
        rt.say(f"Preview ({mode}). Nothing was changed.")
        _print_plan(rt, ready, blocked, remaining)
        rt.say(f"{plural(len(ready), 'email')} would be processed, {len(blocked)} blocked.")
        return 0

    mailer.check_account()
    ready, blocked, remaining = mailer.plan(args.limit)
    if not ready:
        rt.say("Nothing to send: no rows are Approved + Unsent and clean.")
        _print_plan(rt, ready, blocked, remaining)
        if blocked:
            mailer.note_blocked(blocked)  # the sheet is where you work, so the reasons go there too
            rt.say("Fix the blocked rows above (the reasons are also in the Notes column), then run send again.")
        else:
            rt.say("Approve rows in the sheet first (Approval Status = Approved).")
        return 0

    rt.say(f"Mode: {mode}.")
    _print_plan(rt, ready, blocked, remaining)
    if args.live:
        recent = count_sent_since(rt.sheet().read_rows(), rt.clock() - timedelta(hours=24))
        if recent >= 15:
            rt.say(f"Heads up: {recent} emails already went out in the last 24 hours. A new sender does best with a low, steady volume.")
        minutes = round((len(ready) - 1) * (MIN_DELAY_SECONDS + MAX_DELAY_SECONDS) / 2 / 60)
        if len(ready) > 1:
            rt.say(f"There is a random {MIN_DELAY_SECONDS}-{MAX_DELAY_SECONDS} second pause between sends, so this takes about {minutes} minutes. "
                   "Keep this window open and the computer awake (on a Mac: caffeinate -i python main.py send --live). Ctrl+C stops safely.")
        if not args.yes:
            if not rt.interactive:
                raise OutreachError("Live sending needs you at the keyboard to confirm. Run it in a terminal, or add --yes if you are sure.")
            if rt.ask(f"Type SEND to email these {plural(len(ready), 'person', 'people')} for real, anything else cancels: ").strip() != "SEND":
                rt.say("Cancelled. Nothing was sent.")
                return 0

    report = mailer.run(live=args.live, limit=args.limit)
    for o in report.sent:
        rt.say(f"  sent     {clip(o.label, 56)}" + (f"  {o.detail}" if o.detail else ""))
    for o in report.drafted:
        rt.say(f"  drafted  {clip(o.label, 56)}" + (f"  {o.detail}" if o.detail else ""))
    for status in ("blocked", "skipped", "failed", "reverted", "unknown"):
        for o in getattr(report, status):
            rt.say(f"  {status.upper():8s} {clip(o.label, 48)}: {o.detail}")
    if args.live:
        rt.say(f"Sent {plural(len(report.sent), 'email')}.")
    else:
        rt.say(f"Created {plural(len(report.drafted), 'Gmail draft')}. Open Gmail as {rt.settings.sender.email}, go to Drafts, read each one, and press Send.")
        rt.say("When you have sent them, run: python main.py sync")
    if report.remaining:
        rt.say(f"{plural(report.remaining, 'approved row')} left for the next run.")
    if report.aborted:
        rt.say(f"Stopped: {report.aborted}")
    if report.unknown:
        rt.say("IMPORTANT: some outcomes are unknown. Check the Gmail Sent folder / Drafts, then fix those rows by hand as the Notes column says.")
    return finish_code(aborted=report.aborted if not report.interrupted else "", problem=bool(report.unknown))


def cmd_sync(rt: Runtime, args: argparse.Namespace) -> int:
    mailer = Mailer(rt.settings, rt.sheet(), rt.gmail(), sleep=rt.sleep, clock=rt.clock)
    report = mailer.sync_drafts()
    for label in report.now_sent:
        rt.say(f"  marked Sent: {label}")
    rt.say(f"Checked {plural(report.checked, 'draft')}: {len(report.now_sent)} left Gmail Drafts (marked Sent), {report.still_drafts} still waiting for you to send.")
    if report.aborted:
        rt.say(f"Stopped early: {report.aborted}")
    return finish_code(aborted=report.aborted)


# --------------------------------------------------------------------------- status
STATES: list[tuple[str, str, str]] = [
    ("waiting", "waiting for a draft", "python main.py draft"),
    ("review", "drafted, waiting for YOUR review in the sheet", "set Approval Status to Approved or Rejected"),
    ("ready", "approved, ready to send", "python main.py send"),
    ("drafts", "Gmail drafts waiting for you to press Send", "then run: python main.py sync"),
    ("sending", "STUCK in Sending (outcome unknown)", "check the Gmail Sent folder, then set Send Status to Sent or Unsent"),
    ("failed", "failed", "read the Notes column"),
    ("sent", "sent", ""),
    ("rejected", "rejected", ""),
    ("odd", "unrecognised status text", "fix the spelling in Approval Status / Send Status"),
]


def classify(row: LeadRow) -> str:
    approval, send = row.approval, row.send_status
    if same(approval, Approval.REJECTED):
        return "rejected"
    if not send:
        send = SendStatus.UNSENT
    if not approval or same(approval, Approval.PENDING):
        return "waiting" if same(send, SendStatus.UNSENT) else "odd"
    if same(approval, Approval.PENDING_APPROVAL):
        return "review" if same(send, SendStatus.UNSENT) else "odd"
    if same(approval, Approval.APPROVED):
        for key, status in (("ready", SendStatus.UNSENT), ("drafts", SendStatus.DRAFTED), ("sending", SendStatus.SENDING),
                            ("failed", SendStatus.FAILED), ("sent", SendStatus.SENT)):
            if same(send, status):
                return key
    return "odd"


def cmd_status(rt: Runtime, args: argparse.Namespace) -> int:
    rows = rt.sheet().read_rows()
    if not rows:
        rt.say("The sheet has no rows yet. Add some: python main.py import --csv data/targets.csv  (or: discover)")
        rt.say(sheet_hint(rt))
        return 0
    tally: dict[str, int] = {}
    for row in rows:
        key = classify(row)
        tally[key] = tally.get(key, 0) + 1
    rt.say(f"{plural(len(rows), 'row')} in '{rt.sheet().title}':")
    todo: list[str] = []
    for key, label, hint in STATES:
        if tally.get(key):
            rt.say(f"  {tally[key]:3d}  {label}")
            if hint:
                todo.append(f"{label}: {hint}")
    recent = count_sent_since(rows, rt.clock() - timedelta(hours=24))
    if recent:
        rt.say(f"  ({plural(recent, 'email')} sent in the last 24 hours)")
    if todo:
        rt.say("What to do next:")
        for line in todo:
            rt.say(f"  - {line}")
    rt.say(sheet_hint(rt))
    return 0


# --------------------------------------------------------------------------- doctor
Check = tuple[str, str, str]  # (level, title, detail)  level: ok | todo | FAIL | skip


def _git_hooks_path() -> str:
    try:
        out = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def offline_checks(rt: Runtime) -> list[Check]:
    from config.credentials import client_file_problem, token_problem
    s = rt.settings
    checks: list[Check] = []
    version = ".".join(str(n) for n in sys.version_info[:3])
    checks.append(("ok" if sys.version_info >= MIN_PYTHON else "FAIL", f"Python {version}",
                   "" if sys.version_info >= MIN_PYTHON else "needs 3.10 or newer. Use the project's virtualenv: .venv/bin/python main.py doctor"))
    missing = []
    for module, package in REQUIRED_MODULES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    checks.append(("ok", "Required packages installed", "") if not missing else
                  ("FAIL", "Missing packages: " + ", ".join(missing), ".venv/bin/python -m pip install -r requirements.txt"))
    checks.append(("ok", f"Sender: {s.sender.from_header}", f"team {s.sender.team_instagram}, personal {s.sender.personal_instagram}"))
    checks.append(("ok", f"Limits: at most {s.max_per_run} emails per run, {MIN_DELAY_SECONDS}-{MAX_DELAY_SECONDS}s between live sends", ""))
    checks.append(("ok" if rt.env_path.exists() else "todo", ".env file" + (" found" if rt.env_path.exists() else " not found"),
                   "" if rt.env_path.exists() else "python main.py configure"))

    label, env_name, _, _ = KEY_HINTS[s.llm_provider]
    key = s.anthropic_api_key if s.llm_provider == "anthropic" else s.gemini_api_key
    model = s.anthropic_model if s.llm_provider == "anthropic" else s.gemini_model
    checks.append(("ok", f"{label} key set ({mask(key)}), model {model}", "") if key else
                  ("todo", f"{label} API key not set ({env_name})", "python main.py configure"))

    problem = client_file_problem(CREDENTIALS_FILE)
    checks.append(("ok", "Google OAuth client file (config/credentials.json)", "") if not problem else ("todo", "Google OAuth client file", problem))
    problem = token_problem(TOKEN_FILE)
    checks.append(("ok", "Signed in to Google (config/token.json)", "") if not problem else ("todo", "Google sign-in", problem))
    checks.append(("ok", "Review sheet configured", "") if s.spreadsheet_id else ("todo", "No review sheet yet", "python main.py init-sheet"))

    for name, key_value in (("Hunter", s.hunter_api_key), ("Apollo", s.apollo_api_key)):
        checks.append(("ok" if key_value else "skip", f"{name} key " + ("set" if key_value else "not set (optional: the CSV importer works without it)"), ""))
    checks.append(("ok" if DEFAULT_TARGETS_CSV.exists() else "skip",
                   "data/targets.csv " + ("found" if DEFAULT_TARGETS_CSV.exists() else "not created yet (optional)"),
                   "" if DEFAULT_TARGETS_CSV.exists() else "cp data/targets.example.csv data/targets.csv"))
    hooks = _git_hooks_path()
    checks.append(("ok", "Secrets guard is on (git hook)", "") if hooks.rstrip("/").endswith("scripts/hooks") else
                  ("todo", "Secrets guard is off", "git config core.hooksPath scripts/hooks  (this repository is public)"))
    return checks


def online_checks(rt: Runtime) -> list[Check]:
    s = rt.settings
    checks: list[Check] = []

    def attempt(title: str, fn: Callable[[], str]) -> bool:
        try:
            checks.append(("ok", title, fn()))
            return True
        except OutreachError as exc:
            checks.append(("FAIL", title, str(exc)))
        except Exception as exc:  # network and library surprises are reported, not raised
            checks.append(("FAIL", title, f"{exc.__class__.__name__}: {exc}"))
        return False

    if (s.anthropic_api_key if s.llm_provider == "anthropic" else s.gemini_api_key):
        attempt(f"{KEY_HINTS[s.llm_provider][0]} key and model", lambda: rt.llm().check())
    else:
        checks.append(("skip", "Model check", "no API key yet"))

    def google() -> str:
        rt.creds()
        email = rt.gmail().profile_email()
        if email != s.sender.email.lower():
            raise OutreachError(f"signed in as {email}, but this project sends from {s.sender.email}. Delete config/token.json and run: python main.py auth")
        return f"signed in as {email}"

    signed_in = attempt("Google login and Gmail", google)

    def sheet_check() -> str:
        sheet = rt.sheet()
        rows = sheet.read_rows()
        return f"'{sheet.title}' opens, {plural(len(rows), 'lead row')}"

    if signed_in and s.spreadsheet_id:
        attempt("Review sheet", sheet_check)
    else:
        checks.append(("skip", "Review sheet", "needs the Google login and a sheet first"))

    def hunter() -> str:
        fetch = getattr(rt.backend("hunter"), "balance", None)
        balance = fetch() if fetch else None
        if balance is None:
            return "key accepted (the credit balance was not in the reply)"
        return (f"plan {balance.plan or 'unknown'}: {balance.remaining:g} of {balance.available:g} credits left"
                + (f", resets {balance.reset_date}" if balance.reset_date else ""))

    if s.hunter_api_key:
        attempt("Hunter key and credits", hunter)
    return checks


def cmd_doctor(rt: Runtime, args: argparse.Namespace) -> int:
    rt.say("Operation Sponsorships: setup check\n")
    checks = offline_checks(rt)
    if args.online:
        checks += online_checks(rt)
    marks = {"ok": "[ ok ]", "todo": "[todo]", "FAIL": "[FAIL]", "skip": "[skip]"}
    for level, title, detail in checks:
        rt.say(f"{marks[level]} {title}" + (f"\n         {detail}" if detail else ""))
    todo = sum(1 for c in checks if c[0] == "todo")
    failed = sum(1 for c in checks if c[0] == "FAIL")
    rt.say("")
    if failed:
        rt.say(f"{plural(failed, 'problem')} to fix before this will work.")
    elif todo:
        rt.say(f"{plural(todo, 'setup step')} left. Nothing is broken.")
    else:
        rt.say("Everything checks out." + ("" if args.online else " Add --online to also test the keys and logins (free, no emails sent)."))
    return 1 if failed else 0


# --------------------------------------------------------------------------- parser and entry point
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python main.py", description="Penn State Men's Club Volleyball sponsorship outreach. Every email is approved by a human first.")
    parser.add_argument("-v", "--verbose", action="store_true", help="show detailed logs")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("configure", help="save your model API key (hidden prompt) to .env")
    p.add_argument("--provider", choices=["anthropic", "gemini"])
    p.add_argument("--key-from-stdin", action="store_true", help="read the key from stdin, for example: pbpaste | python main.py configure --key-from-stdin")
    p.add_argument("--no-check", action="store_true", help="do not test the key after saving")

    p = sub.add_parser("auth", help="sign in to Google as the club account")
    p.add_argument("--no-browser", action="store_true", help="print the link instead of opening a browser")

    p = sub.add_parser("init-sheet", help="create the review sheet (or --repair an existing one)")
    p.add_argument("--repair", action="store_true", help="add missing columns and refresh dropdowns on the configured sheet")
    p.add_argument("--new", action="store_true", help="create another sheet even though one is configured")
    p.add_argument("--title", default=DEFAULT_TITLE)
    p.add_argument("--share", action="append", metavar="EMAIL", help="give this Google account edit access (repeatable)")

    for name, text in (("import", "add contacts that already have emails"), ("discover", "find contacts at brands with Hunter or Apollo and add them")):
        p = sub.add_parser(name, help=text)
        source = p.add_mutually_exclusive_group()
        source.add_argument("--csv", metavar="PATH", help="default: data/targets.csv")
        source.add_argument("--sheet", metavar="URL_OR_ID", help="another Google Sheet to read the list from")
        p.add_argument("--tab", help="tab name when using --sheet (default: the first tab)")
        if name == "discover":
            p.add_argument("--backend", choices=["hunter", "apollo"], help="default: DISCOVERY_BACKEND in .env")
            p.add_argument("--per-domain", type=int, default=disc.PER_DOMAIN_DEFAULT, metavar="N", help="contacts to keep per brand (default %(default)s)")
            p.add_argument("--limit", type=int, default=disc.LOOKUP_LIMIT_DEFAULT, metavar="N", help="brands to look up per run (default %(default)s)")
            p.add_argument("--min-confidence", type=int, default=disc.MIN_CONFIDENCE_DEFAULT, metavar="N", help="drop addresses below this confidence (default %(default)s)")
            p.add_argument("--no-broad", action="store_true", help="Hunter: skip the second, wider search when no exact title matches")
            p.add_argument("--max-reveals", type=int, default=20, metavar="N", help="Apollo: most emails to unlock this run (default %(default)s)")
            p.add_argument("--recheck-days", type=int, default=disc.RECHECK_DAYS_DEFAULT, metavar="N",
                           help="skip brands searched with no usable contact in the last N days (default %(default)s; 0 searches everything again)")
            p.add_argument("--dry-run", action="store_true", help="show what would be looked up; spend nothing, add nothing")

    p = sub.add_parser("try-draft", help="write one sample email with the real model (no Google login needed; saves and sends nothing)")
    p.add_argument("--company", default="CeraVe", help="default: %(default)s")
    p.add_argument("--category", default="Skincare", help="Energy, Skincare, Hydration, Recovery or Gear (default %(default)s)")
    p.add_argument("--name", default="Jane Doe", help="contact name, used only for the greeting and never sent to the model (default %(default)s)")
    p.add_argument("--hook", help="optional Custom Hook / Angle note for the opening")
    p.add_argument("--show-prompt", action="store_true", help="also print exactly what is sent to the model")
    p.add_argument("--manual", action="store_true", help="copy the prompt to your clipboard; paste Gemini Web's reply back instead of calling the API")

    p = sub.add_parser("draft", help="write emails for Pending rows")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, metavar="N", help="most emails to write this run (default %(default)s)")
    p.add_argument("--row", action="append", metavar="ROW_ID", help="draft only this Row ID (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="print the emails without writing them to the sheet")
    p.add_argument("--manual", action="store_true", help="copy each prompt to your clipboard; paste Gemini Web's reply back instead of calling the API")

    p = sub.add_parser("send", help="create Gmail drafts for Approved rows (add --live to really send)")
    p.add_argument("--live", action="store_true", help="actually send. Without this, only Gmail drafts are created")
    p.add_argument("--limit", type=int, metavar="N", help="lower the per-run cap (never raises it above 15)")
    p.add_argument("--yes", action="store_true", help="skip the typed confirmation for --live")
    p.add_argument("--preview", action="store_true", help="show what would happen and change nothing")

    sub.add_parser("sync", help="mark rows Sent once their Gmail draft has been sent by hand")
    sub.add_parser("status", help="where every row stands and what to do next")
    p = sub.add_parser("doctor", help="check the setup and say exactly what is missing")
    p.add_argument("--online", action="store_true", help="also test the keys and logins (free; sends nothing)")
    return parser


COMMANDS: dict[str, Callable[[Runtime, argparse.Namespace], int]] = {
    "configure": cmd_configure, "auth": cmd_auth, "init-sheet": cmd_init_sheet, "import": cmd_import, "discover": cmd_discover,
    "try-draft": cmd_try_draft, "draft": cmd_draft, "send": cmd_send, "sync": cmd_sync, "status": cmd_status, "doctor": cmd_doctor,
}


def main(argv: Sequence[str] | None = None, *, runtime: Runtime | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if runtime is None:
        setup_logging(args.verbose)
    try:
        rt = runtime or Runtime(Settings.load())
        return COMMANDS[args.command](rt, args)
    except OutreachError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped by you (Ctrl+C).", file=sys.stderr)
        return 130
    except Exception as exc:
        log.exception("Unexpected error in '%s'", args.command)
        print(f"Unexpected error ({exc.__class__.__name__}): {exc}\nDetails were saved to {rel(LOGS_DIR)}/outreach.log", file=sys.stderr)
        return 2
