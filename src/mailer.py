"""Gmail dispatcher. Two modes:

    drafts (default)  creates a Gmail draft for each approved row. Nothing is sent: you open Gmail,
                      read it, and press Send yourself.
    live (--live)     sends for real, at most 15 per run, with a random 180 to 420 second pause
                      between sends.

Only rows with Approval Status = Approved and Send Status = Unsent are ever touched.

How duplicate sends are prevented (the worst failure for cold outreach):
  * A live row is marked Sending BEFORE the Gmail call and re-checked at that moment, so a row you
    flipped to Rejected a second ago is not sent.
  * Rows stuck in Sending (crash, lost connection) are never retried automatically.
  * The send call is never repeated. Gmail calls use a plain requests session (no hidden retry of
    POSTs). If the outcome is unknown (timeout, dropped connection, 5xx) the row stays in Sending,
    the run stops, and you are told to check the Gmail Sent folder.
  * The same address is never mailed twice (per sheet, and within a run).
"""
from __future__ import annotations

import base64
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr
from typing import Any, Callable, Sequence

import requests
from google.auth.exceptions import RefreshError, TransportError
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError

from config.errors import MailError, RowChanged, RowNotFound, SheetError
from config.google_errors import explain_google_error, parse_google_error
from config.settings import (
    HARD_CAP_PER_RUN, MAX_DELAY_SECONDS, MIN_DELAY_SECONDS, Approval, Col, Sender, SendStatus, Settings,
)
from src.guardrails import lint_for_send
from src.models import LeadRow, normalize_email
from src.sheet_manager import SheetManager, same, tool_note

log = logging.getLogger(__name__)

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
REQUEST_TIMEOUT = (10, 60)  # (connect, read) seconds
NOTE_TAG = "send"
CONSECUTIVE_FAILURE_ABORT = 3


# --------------------------------------------------------------------------- Gmail errors
class GmailCallError(MailError):
    """A Gmail call did not succeed. `kind` says what that means for the row:

    row      Gmail rejected this message. Definitely not sent. Mark the row Failed and carry on.
    run      Definitely not sent, and the cause is the account, quota or network, so every later
             row would fail too. Put the row back to Unsent and stop the run.
    unknown  The message MAY have been sent (timeout, dropped connection, 5xx). Leave the row in
             Sending, stop the run, and have a human check the Sent folder.
    """

    def __init__(self, kind: str, message: str, status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status


def classify_status(status: int, reason: str = "") -> str:
    if status >= 500 or status in (408, 409):
        return "unknown"
    if status in (401, 403, 404, 429) or (status == 400 and reason == "failedPrecondition"):
        return "run"
    return "row"


def request_never_left(exc: BaseException) -> bool:
    """True only when the failure happened before any bytes of the request could be sent."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        for arg in exc.args:
            reason = getattr(arg, "reason", arg)
            if isinstance(reason, (NewConnectionError, NameResolutionError, ConnectTimeoutError)):
                return True
    return False


class GmailApi:
    """The three Gmail calls we need, over an authorised requests session."""

    def __init__(self, session: Any, *, timeout: tuple[float, float] = REQUEST_TIMEOUT, base_url: str = GMAIL_BASE):
        self._session = session
        self._timeout = timeout
        self._base = base_url.rstrip("/")

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self._base + path
        try:
            # "Connection: close" gives every call a fresh socket. A laptop that slept between two
            # sends could otherwise hand us a dead pooled connection and turn a clean send into an
            # ambiguous one.
            response = self._session.request(method, url, json=body, headers={"Connection": "close"}, timeout=self._timeout)
        except (RefreshError, TransportError) as exc:  # raised while getting a token, before sending
            raise GmailCallError(
                "run", "Google would not give a fresh login token. Run: python main.py auth. Nothing was sent."
            ) from exc
        except requests.exceptions.RequestException as exc:
            if request_never_left(exc):
                raise GmailCallError("run", "Could not reach Gmail (no internet or a DNS problem). Nothing was sent.") from exc
            raise GmailCallError(
                "unknown",
                f"The connection to Gmail broke ({type(exc).__name__}) and it is not known whether the request arrived.",
            ) from exc

        if 200 <= response.status_code < 300:
            try:
                data = response.json()
            except ValueError as exc:
                raise GmailCallError("unknown", "Gmail answered but the reply could not be read.") from exc
            return data if isinstance(data, dict) else {}
        status, reason, message = parse_google_error(response.content)
        status = status or response.status_code
        raise GmailCallError(classify_status(status, reason), explain_google_error("gmail", status, reason, message), status)

    def profile_email(self) -> str:
        return str(self._call("GET", "/profile").get("emailAddress", "")).strip().lower()

    def create_draft(self, raw: str) -> str:
        draft_id = self._call("POST", "/drafts", {"message": {"raw": raw}}).get("id")
        if not draft_id:
            raise GmailCallError("unknown", "Gmail did not return a draft ID, so it is unclear whether a draft was created.")
        return str(draft_id)

    def send_raw(self, raw: str) -> str:
        message_id = self._call("POST", "/messages/send", {"raw": raw}).get("id")
        if not message_id:
            raise GmailCallError("unknown", "Gmail did not return a message ID, so it is unclear whether the email was sent.")
        return str(message_id)

    def draft_exists(self, draft_id: str) -> bool:
        try:
            self._call("GET", f"/drafts/{draft_id}")
        except GmailCallError as exc:
            if exc.status == 404:
                return False
            raise
        return True


def build_raw_message(sender: Sender, to_email: str, subject: str, body: str) -> str:
    """RFC 5322 plain-text message, base64url encoded for the Gmail API. Raises ValueError if any
    header would contain a line break."""
    message = EmailMessage(policy=SMTP)
    message["From"] = formataddr((sender.name, sender.email))
    message["To"] = to_email
    message["Subject"] = subject.strip()
    message.set_content(body.replace("\r\n", "\n").strip() + "\n")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


# --------------------------------------------------------------------------- results
@dataclass
class MailOutcome:
    row_id: str
    label: str
    status: str  # sent | drafted | blocked | skipped | failed | reverted | unknown
    detail: str = ""
    gmail_id: str = ""


@dataclass
class MailReport:
    live: bool
    outcomes: list[MailOutcome] = field(default_factory=list)
    remaining: int = 0  # eligible rows left because the per-run cap was reached
    aborted: str = ""
    interrupted: bool = False

    def _by(self, status: str) -> list[MailOutcome]:
        return [o for o in self.outcomes if o.status == status]

    sent = property(lambda self: self._by("sent"))
    drafted = property(lambda self: self._by("drafted"))
    blocked = property(lambda self: self._by("blocked"))
    skipped = property(lambda self: self._by("skipped"))
    failed = property(lambda self: self._by("failed"))
    reverted = property(lambda self: self._by("reverted"))
    unknown = property(lambda self: self._by("unknown"))


@dataclass
class SyncReport:
    checked: int = 0
    now_sent: list[str] = field(default_factory=list)  # labels of rows whose draft left Gmail Drafts
    still_drafts: int = 0
    aborted: str = ""


def local_now() -> datetime:
    return datetime.now().astimezone()


def count_sent_since(rows: Sequence[LeadRow], since: datetime) -> int:
    """Rows whose Sent Timestamp is at or after `since`. Unreadable timestamps are ignored."""
    count = 0
    for row in rows:
        stamp = parse_iso(row.get(Col.SENT_AT))
        if stamp and stamp >= since:
            count += 1
    return count


def parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


# --------------------------------------------------------------------------- mailer
class Mailer:
    def __init__(
        self,
        settings: Settings,
        sheet: SheetManager,
        gmail: GmailApi,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = local_now,
    ):
        self.settings = settings
        self.sheet = sheet
        self.gmail = gmail
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock
        self._interrupted: MailOutcome | None = None

    # ---- helpers
    def cap(self, limit: int | None = None) -> int:
        """Rows per run: the configured cap, lowered (never raised) by `limit`, never above 15."""
        cap = min(self.settings.max_per_run, HARD_CAP_PER_RUN)
        return max(1, min(cap, limit)) if limit else cap

    def check_account(self) -> str:
        """Refuse to run unless the saved login is the club's Gmail account."""
        actual = (self.gmail.profile_email() or "").strip().lower()
        wanted = self.settings.sender.email.strip().lower()
        if actual != wanted:
            raise MailError(
                f"The saved Google login is {actual or 'an unknown account'}, but this project sends from {wanted}. "
                f"Nothing was done. Delete config/token.json and run: python main.py auth (sign in as {wanted})."
            )
        return actual

    @staticmethod
    def _eligible(rows: Sequence[LeadRow]) -> list[LeadRow]:
        """Approved rows that have not been handled. A blank Send Status counts as Unsent, so a
        hand-typed row previews the same way it will be processed."""
        return [r for r in rows if same(r.approval, Approval.APPROVED) and (not r.send_status or same(r.send_status, SendStatus.UNSENT))]

    def eligible_rows(self) -> list[LeadRow]:
        """Approved + Unsent rows in sheet order (read only; no tidy-up writes)."""
        return self._eligible(self.sheet.read_rows())

    def sent_in_last_24h(self) -> int:
        return count_sent_since(self.sheet.read_rows(), self._clock() - timedelta(hours=24))

    @staticmethod
    def _contacted(rows: Sequence[LeadRow]) -> set[str]:
        """Addresses already emailed, drafted, or mid-send on some row."""
        busy = (SendStatus.SENT, SendStatus.SENDING, SendStatus.DRAFTED)
        return {normalize_email(r.email) for r in rows if r.email and any(same(r.send_status, b) for b in busy)}

    def plan(self, limit: int | None = None) -> tuple[list[LeadRow], list[tuple[LeadRow, list[str]]], int]:
        """Read-only preview: (rows that would be processed, blocked rows with reasons, rows left by the cap).
        Writes nothing."""
        everything = self.sheet.read_rows()
        contacted = self._contacted(everything)
        cap = self.cap(limit)
        ready: list[LeadRow] = []
        blocked: list[tuple[LeadRow, list[str]]] = []
        remaining = 0
        for row in self._eligible(everything):
            problems = self._problems(row, contacted)
            if problems:
                blocked.append((row, problems))
            elif len(ready) >= cap:
                remaining += 1
            else:
                ready.append(row)
                contacted.add(normalize_email(row.email))
        return ready, blocked, remaining

    def _problems(self, row: LeadRow, contacted: set[str]) -> list[str]:
        problems = lint_for_send(row.subject, row.body, row.email, self.settings.banned_brands)
        email = normalize_email(row.email)
        if email and email in contacted:
            problems.append("this address was already emailed (or drafted) from another row")
        return problems

    def _note(self, row: LeadRow, message: str) -> dict[str, str]:
        had_ours = any(line.startswith(f"[{NOTE_TAG}]") for line in row.notes.splitlines())
        if not message and not had_ours:
            return {}
        new = tool_note(row.notes, NOTE_TAG, message)
        return {Col.NOTES: new} if new != row.notes else {}

    def _write(self, row_id: str, updates: dict[str, str], expect: dict[str, str] | None = None) -> LeadRow | None:
        """Best-effort sheet write for housekeeping. Returns None if the row is gone or was edited."""
        try:
            return self.sheet.update_row(row_id, updates, expect=expect)
        except (RowChanged, RowNotFound):
            return None

    # ---- main entry
    def run(self, *, live: bool = False, limit: int | None = None) -> MailReport:
        report = MailReport(live=live)
        self.check_account()
        for where, column, value in self.sheet.prepare_rows().unknown_values:
            log.warning("%s: '%s' in %s is not a known status, so this row is left alone.", where, value, column)

        rows = self.eligible_rows()
        cap = self.cap(limit)
        contacted = self._contacted(self.sheet.read_rows())
        attempted = 0
        failure_streak = 0
        try:
            for row in rows:
                problems = self._problems(row, contacted)
                if problems:
                    self._block(row, problems, report)
                    continue
                if attempted >= cap:
                    report.remaining += 1
                    continue
                if live and attempted > 0:
                    delay = self._rng.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
                    log.info("Waiting %d seconds before the next send (%d of %d done). Ctrl+C stops safely.", delay, attempted, cap)
                    self._sleep(delay)
                attempted += 1
                outcome, stop = self._process(row, live, contacted)
                report.outcomes.append(outcome)
                if outcome.status in ("sent", "drafted"):
                    failure_streak = 0
                    contacted.add(normalize_email(row.email))
                elif outcome.status == "failed":
                    failure_streak += 1
                if stop:
                    report.aborted = outcome.detail
                    break
                if failure_streak >= CONSECUTIVE_FAILURE_ABORT:
                    report.aborted = f"{CONSECUTIVE_FAILURE_ABORT} rows in a row failed, so the run was stopped. Check the Notes column."
                    break
        except KeyboardInterrupt:
            report.interrupted = True
            report.aborted = "Stopped by you (Ctrl+C)."
            if self._interrupted:
                report.outcomes.append(self._interrupted)
        return report

    # ---- one row
    def _note_blocked(self, row: LeadRow, problems: Sequence[str]) -> str:
        """Write why a row was not sent into its Notes cell, never over a note a human just edited."""
        text = "; ".join(problems)
        update = self._note(row, f"not sent: {text}. Fix the row and it goes out on the next run.")
        if update:
            self._write(row.row_id, update, expect={Col.NOTES: row.notes})
        return text

    def note_blocked(self, blocked: Sequence[tuple[LeadRow, Sequence[str]]]) -> None:
        """Record in Notes why each blocked row was not sent. Writes notes only: sends and drafts nothing."""
        for row, problems in blocked:
            self._note_blocked(row, problems)

    def _block(self, row: LeadRow, problems: Sequence[str], report: MailReport) -> None:
        text = self._note_blocked(row, problems)
        report.outcomes.append(MailOutcome(row.row_id, row.label(), "blocked", text))

    def _process(self, listed: LeadRow, live: bool, contacted: set[str]) -> tuple[MailOutcome, bool]:
        """Returns (outcome, stop_the_run)."""
        row_id, label = listed.row_id, listed.label()
        try:
            row = self.sheet.get_row(row_id)
        except RowNotFound:
            return MailOutcome(row_id, label, "skipped", "the row was deleted"), False
        if not (same(row.approval, Approval.APPROVED) and same(row.send_status, SendStatus.UNSENT)):
            return MailOutcome(row_id, label, "skipped", f"changed to {row.approval or 'blank'} / {row.send_status or 'blank'} since the run started"), False
        problems = self._problems(row, contacted)
        if problems:
            return MailOutcome(row_id, label, "blocked", self._note_blocked(row, problems)), False
        try:
            raw = build_raw_message(self.settings.sender, normalize_email(row.email), row.subject, row.body)
        except ValueError as exc:
            self._write(row_id, {Col.SEND: SendStatus.FAILED, **self._note(row, f"could not build the email: {exc}")})
            return MailOutcome(row_id, label, "failed", f"could not build the email: {exc}"), False
        return self._deliver_live(row, raw) if live else self._deliver_draft(row, raw)

    def _deliver_draft(self, row: LeadRow, raw: str) -> tuple[MailOutcome, bool]:
        row_id, label = row.row_id, row.label()
        try:
            draft_id = self.gmail.create_draft(raw)
        except GmailCallError as exc:
            return self._failed_call(row, exc, claimed=False, what="create the draft")
        updates = {Col.SEND: SendStatus.DRAFTED, Col.GMAIL_ID: draft_id, **self._note(row, "")}
        try:
            self.sheet.update_row(row_id, updates)
        except (RowChanged, RowNotFound, SheetError) as exc:
            msg = f"The draft was created in Gmail but the sheet could not be updated ({exc}). Delete the draft or mark the row by hand."
            return MailOutcome(row_id, label, "drafted", msg, draft_id), True
        log.info("Draft created for %s", label)
        return MailOutcome(row_id, label, "drafted", gmail_id=draft_id), False

    def _deliver_live(self, row: LeadRow, raw: str) -> tuple[MailOutcome, bool]:
        row_id, label = row.row_id, row.label()
        # 1. Claim the row. From here a crash can only leave it in Sending, which is never retried.
        try:
            claimed = self.sheet.update_row(
                row_id, {Col.SEND: SendStatus.SENDING},
                expect={Col.APPROVAL: Approval.APPROVED, Col.SEND: SendStatus.UNSENT},
            )
        except RowChanged as exc:
            return MailOutcome(row_id, label, "skipped", f"changed just before sending, so it was not sent ({exc})"), False
        except RowNotFound:
            return MailOutcome(row_id, label, "skipped", "the row was deleted"), False
        # 2. The text we send must be exactly what we checked.
        if (claimed.subject, claimed.body, normalize_email(claimed.email)) != (row.subject, row.body, normalize_email(row.email)):
            self._write(row_id, {Col.SEND: SendStatus.UNSENT}, expect={Col.SEND: SendStatus.SENDING})
            return MailOutcome(row_id, label, "skipped", "edited at the last moment, so it was not sent. It will be checked again next run."), False
        # 3. Send. Never retried.
        try:
            message_id = self.gmail.send_raw(raw)
        except GmailCallError as exc:
            return self._failed_call(row, exc, claimed=True, what="send")
        except KeyboardInterrupt:
            text = (f"Interrupted (Ctrl+C) while sending to {row.email}. It may or may not have gone out. "
                    "CHECK the Gmail Sent folder: if it is there set Send Status to Sent, otherwise to Unsent.")
            self._write(row_id, self._note(claimed, text))
            self._interrupted = MailOutcome(row_id, label, "unknown", text)
            raise
        # 4. Record it.
        stamp = self._clock().isoformat(timespec="seconds")
        updates = {Col.SEND: SendStatus.SENT, Col.SENT_AT: stamp, Col.GMAIL_ID: message_id, **self._note(claimed, "")}
        try:
            self.sheet.update_row(row_id, updates)
        except (RowChanged, RowNotFound, SheetError) as exc:
            msg = (f"SENT to {row.email} (Gmail ID {message_id}) but the sheet could not be updated ({exc}). The row stays in "
                   "Sending so it will not go out again. Set it to Sent by hand.")
            return MailOutcome(row_id, label, "sent", msg, message_id), True
        log.info("Sent to %s (%s)", row.email, label)
        return MailOutcome(row_id, label, "sent", gmail_id=message_id), False

    def _failed_call(self, row: LeadRow, exc: GmailCallError, *, claimed: bool, what: str) -> tuple[MailOutcome, bool]:
        row_id, label = row.row_id, row.label()
        if exc.kind == "row":
            self._write(row_id, {Col.SEND: SendStatus.FAILED, **self._note(row, f"Gmail rejected it: {exc}")})
            return MailOutcome(row_id, label, "failed", str(exc)), False
        if exc.kind == "run":
            updates = {Col.SEND: SendStatus.UNSENT, **self._note(row, f"not sent: {exc}")} if claimed else self._note(row, f"not sent: {exc}")
            self._write(row_id, updates, expect={Col.SEND: SendStatus.SENDING} if claimed else None)
            return MailOutcome(row_id, label, "reverted", f"Could not {what}: {exc} The row is unchanged and will be retried next run."), True
        # unknown outcome
        if claimed:
            self._write(row_id, self._note(row, f"UNKNOWN: {exc} CHECK the Gmail Sent folder for {row.email}. If it is there set Send Status to Sent, otherwise to Unsent."))
            detail = f"Could not confirm whether the email to {row.email} was sent: {exc} The row stays in Sending. Check the Gmail Sent folder."
        else:
            self._write(row_id, self._note(row, f"a draft may or may not exist in Gmail: {exc}"))
            detail = f"Could not confirm the draft was created: {exc} Check your Gmail Drafts for a duplicate before running again."
        return MailOutcome(row_id, label, "unknown", detail), True

    # ---- reconcile drafts you sent by hand
    def sync_drafts(self) -> SyncReport:
        """Mark a Drafted row Sent when its draft has left Gmail Drafts (you sent it, or deleted it)."""
        report = SyncReport()
        self.check_account()
        today = self._clock().date().isoformat()
        for row in self.sheet.read_rows():
            if not same(row.send_status, SendStatus.DRAFTED):
                continue
            draft_id = row.get(Col.GMAIL_ID)
            if not draft_id:
                continue
            report.checked += 1
            try:
                exists = self.gmail.draft_exists(draft_id)
            except GmailCallError as exc:
                report.aborted = str(exc)
                break
            if exists:
                report.still_drafts += 1
                continue
            note = tool_note(row.notes, NOTE_TAG, f"the draft left Gmail Drafts by {today} (sent or deleted by hand), so this is marked Sent. If you deleted it instead, set Send Status back to Unsent.")
            if self._write(row.row_id, {Col.SEND: SendStatus.SENT, Col.NOTES: note}, expect={Col.SEND: SendStatus.DRAFTED}):
                report.now_sent.append(row.label())
        return report
