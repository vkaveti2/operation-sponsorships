"""Google Sheet review queue: the one place rows are read and written.

Safety rules baked in here:
  * Columns are found by header text, never by position, so you can reorder them freely.
  * Rows are found by Row ID right before every write. Sorting or filtering the sheet while a
    run is in progress cannot make us write to the wrong lead.
  * update_row(expect=...) refuses to write if a human changed a guarded cell since we read it.
  * Only the cells named in `updates` are written. Your edits to other cells are never touched.
  * Values go in as RAW text, so nothing typed or generated can turn into a formula.
"""
from __future__ import annotations

import logging
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from gspread.utils import InsertDataOption, ValueInputOption, rowcol_to_a1

from config.errors import RowChanged, RowNotFound, SheetError
from config.google_errors import describe_exception, is_retryable, summarize_exception
from config.settings import CATEGORIES, COLUMNS, Approval, Col, SendStatus, Settings
from src.models import Lead, LeadRow, normalize_category, normalize_email

log = logging.getLogger(__name__)

GRID_ROWS = 1000  # rows covered by dropdowns and formatting; the sheet grows past this fine
RETRY_DELAYS = (2, 5, 12, 30, 60)  # seconds; Sheets quotas reset every minute
PROTECTED_DESCRIPTION = "operation_sponsorships: filled in by the tool, edit with care"
DEFAULT_TITLE = "Operation Sponsorships - Outreach Queue"

COLUMN_WIDTHS = {
    Col.ROW_ID: 85, Col.COMPANY: 160, Col.CATEGORY: 100, Col.TARGET_NAME: 150,
    Col.TARGET_EMAIL: 230, Col.HOOK: 260, Col.SUBJECT: 280, Col.BODY: 560, Col.APPROVAL: 135,
    Col.SEND: 100, Col.SENT_AT: 190, Col.GMAIL_ID: 170, Col.NOTES: 280, Col.DOMAIN: 150,
}
WRAP_COLUMNS = (Col.HOOK, Col.SUBJECT, Col.BODY, Col.NOTES)
TOOL_MANAGED_COLUMNS = (Col.ROW_ID, Col.SENT_AT, Col.GMAIL_ID)

_GREY = {"red": 0.90, "green": 0.90, "blue": 0.90}
_AMBER = {"red": 1.0, "green": 0.95, "blue": 0.70}
_GREEN = {"red": 0.80, "green": 0.94, "blue": 0.80}
_RED = {"red": 0.96, "green": 0.80, "blue": 0.80}
_BLUE = {"red": 0.80, "green": 0.88, "blue": 0.98}
_ORANGE = {"red": 1.0, "green": 0.85, "blue": 0.60}
STATUS_COLORS: dict[str, dict[str, dict[str, float]]] = {
    Col.APPROVAL: {
        Approval.PENDING: _GREY, Approval.PENDING_APPROVAL: _AMBER,
        Approval.APPROVED: _GREEN, Approval.REJECTED: _RED,
    },
    Col.SEND: {
        SendStatus.SENDING: _ORANGE, SendStatus.DRAFTED: _BLUE,
        SendStatus.SENT: _GREEN, SendStatus.FAILED: _RED,
    },
}


# --------------------------------------------------------------------------- small helpers
def same(a: str | None, b: str | None) -> bool:
    """Case-insensitive, whitespace-insensitive comparison for statuses and guarded cells."""
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def tool_note(existing: str, tag: str, message: str) -> str:
    """Replace this tool's earlier [tag] line in a Notes cell and keep the human's own lines."""
    prefix = f"[{tag}]"
    kept = [ln for ln in (existing or "").splitlines() if ln.strip() and not ln.startswith(prefix)]
    if message:
        kept.append(f"{prefix} {message}")
    return "\n".join(kept)


def select_rows(rows: Iterable[LeadRow], *, approval: str | None = None, send: str | None = None) -> list[LeadRow]:
    """Rows whose statuses match exactly (case-insensitive). None means 'do not filter'."""
    return [
        row for row in rows
        if (approval is None or same(row.approval, approval)) and (send is None or same(row.send_status, send))
    ]


def _canonical(value: str, allowed: Sequence[str]) -> str | None:
    for option in allowed:
        if same(value, option):
            return option
    return None


# --------------------------------------------------------------------------- layout requests
def _grid(sheet_id: int, r0: int, r1: int | None, c0: int, c1: int) -> dict[str, int]:
    rng = {"sheetId": sheet_id, "startRowIndex": r0, "startColumnIndex": c0, "endColumnIndex": c1}
    if r1 is not None:
        rng["endRowIndex"] = r1
    return rng


def build_layout_requests(
    sheet_id: int,
    header: Sequence[str],
    *,
    grid_rows: int = GRID_ROWS,
    stale_conditional_formats: Sequence[int] = (),
    stale_protected_ranges: Sequence[int] = (),
) -> list[dict[str, Any]]:
    """Spreadsheets.batchUpdate requests for header style, widths, dropdowns, status colors.

    Pure function so it can be checked against Google's own API schema in tests.
    `stale_conditional_formats` are rule indexes to delete (highest first); `stale_protected_ranges`
    are protectedRangeIds from a previous run of this tool.
    """
    idx = {name: i for i, name in enumerate(header) if name}
    reqs: list[dict[str, Any]] = []

    for rule_index in sorted(stale_conditional_formats, reverse=True):
        reqs.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": rule_index}})
    for range_id in stale_protected_ranges:
        reqs.append({"deleteProtectedRange": {"protectedRangeId": range_id}})

    width = len(header)
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }})
    reqs.append({"repeatCell": {
        "range": _grid(sheet_id, 0, 1, 0, width),
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.13, "green": 0.20, "blue": 0.35},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
    }})
    reqs.append({"repeatCell": {
        "range": _grid(sheet_id, 1, grid_rows, 0, width),
        "cell": {"userEnteredFormat": {"verticalAlignment": "TOP"}},
        "fields": "userEnteredFormat.verticalAlignment",
    }})
    for name in WRAP_COLUMNS:
        if name in idx:
            reqs.append({"repeatCell": {
                "range": _grid(sheet_id, 1, grid_rows, idx[name], idx[name] + 1),
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy",
            }})
    for name, pixels in COLUMN_WIDTHS.items():
        if name in idx:
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": idx[name], "endIndex": idx[name] + 1},
                "properties": {"pixelSize": pixels},
                "fields": "pixelSize",
            }})

    dropdowns = (
        (Col.CATEGORY, CATEGORIES, False),  # soft: other categories allowed, drafter falls back
        (Col.APPROVAL, Approval.ALL, True),
        (Col.SEND, SendStatus.ALL, True),
    )
    for name, options, strict in dropdowns:
        if name in idx:
            reqs.append({"setDataValidation": {
                "range": _grid(sheet_id, 1, grid_rows, idx[name], idx[name] + 1),
                "rule": {
                    "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": o} for o in options]},
                    "strict": strict,
                    "showCustomUi": True,
                },
            }})

    for name, colors in STATUS_COLORS.items():
        if name not in idx:
            continue
        for value, color in colors.items():
            reqs.append({"addConditionalFormatRule": {
                "rule": {
                    "ranges": [_grid(sheet_id, 1, grid_rows, idx[name], idx[name] + 1)],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
                        "format": {"backgroundColor": color},
                    },
                },
                "index": 0,
            }})

    for name in TOOL_MANAGED_COLUMNS:
        if name in idx:
            reqs.append({"addProtectedRange": {"protectedRange": {
                "range": _grid(sheet_id, 1, None, idx[name], idx[name] + 1),
                "description": PROTECTED_DESCRIPTION,
                "warningOnly": True,  # a warning, not a lock: you can still edit
            }}})
    return reqs


# --------------------------------------------------------------------------- results
@dataclass
class AppendResult:
    added_ids: list[str] = field(default_factory=list)
    duplicates: list[Lead] = field(default_factory=list)  # email already in the sheet
    invalid: list[tuple[Lead, str]] = field(default_factory=list)


@dataclass
class PrepareResult:
    ids_assigned: int = 0
    defaults_filled: int = 0
    canonicalised: int = 0
    unknown_values: list[tuple[str, str, str]] = field(default_factory=list)  # (row label, column, value)

    @property
    def changed(self) -> int:
        return self.ids_assigned + self.defaults_filled + self.canonicalised


@dataclass
class Snapshot:
    header: list[str]
    index: dict[str, int]
    rows: list[LeadRow]


# --------------------------------------------------------------------------- manager
class SheetManager:
    def __init__(self, spreadsheet: Any, worksheet: Any, *, sleep: Callable[[float], None] = time.sleep):
        self._spreadsheet = spreadsheet
        self._ws = worksheet
        self._sleep = sleep

    # ---- construction
    @classmethod
    def connect(cls, settings: Settings, client: Any, *, sleep: Callable[[float], None] = time.sleep) -> "SheetManager":
        sheet_id = settings.require_sheet()
        try:
            spreadsheet = client.open_by_key(sheet_id)
        except SpreadsheetNotFound as exc:
            raise SheetError(
                "Google could not find that spreadsheet. Check SPREADSHEET_ID in .env, and that "
                "the Google account you signed in with can open it."
            ) from exc
        except APIError as exc:
            raise SheetError(summarize_exception(exc, "sheets")) from exc
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            raise SheetError("Could not reach Google Sheets. Check your internet connection.") from exc
        try:
            worksheet = spreadsheet.worksheet(settings.worksheet_name)
        except WorksheetNotFound as exc:
            tabs = ", ".join(f"'{w.title}'" for w in spreadsheet.worksheets()) or "none"
            raise SheetError(
                f"The tab '{settings.worksheet_name}' does not exist in '{spreadsheet.title}' "
                f"(tabs: {tabs}). Fix WORKSHEET_NAME in .env or run: python main.py init-sheet"
            ) from exc
        except APIError as exc:
            raise SheetError(summarize_exception(exc, "sheets")) from exc
        return cls(spreadsheet, worksheet, sleep=sleep)

    @classmethod
    def create_new(
        cls,
        settings: Settings,
        client: Any,
        *,
        title: str = DEFAULT_TITLE,
        share_with: Sequence[str] = (),
        sleep: Callable[[float], None] = time.sleep,
    ) -> "SheetManager":
        """Create the spreadsheet, rename the first tab, write the header, dropdowns, colors."""
        try:
            spreadsheet = client.create(title)
        except APIError as exc:
            raise SheetError(summarize_exception(exc, "drive")) from exc
        worksheet = spreadsheet.sheet1
        worksheet.update_title(settings.worksheet_name)
        manager = cls(spreadsheet, worksheet, sleep=sleep)
        manager.ensure_layout()
        for email in share_with:
            try:
                spreadsheet.share(
                    email, perm_type="user", role="writer", notify=True,
                    email_message="Review queue for Penn State Men's Club Volleyball sponsorship outreach.",
                )
            except APIError as exc:
                log.warning("Could not share the sheet with %s: %s", email, summarize_exception(exc, "drive"))
        return manager

    # ---- identity
    @property
    def url(self) -> str:
        return str(self._spreadsheet.url)

    @property
    def spreadsheet_id(self) -> str:
        return str(self._spreadsheet.id)

    @property
    def title(self) -> str:
        return str(self._spreadsheet.title)

    # ---- low-level call with retry
    def _call(self, fn: Callable[..., Any], *args: Any, idempotent: bool = True, **kwargs: Any) -> Any:
        """Run one Sheets call. Quota errors (429) are always retried. Network errors and 5xx are
        retried only for idempotent calls: re-sending an append could add rows twice."""
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                return fn(*args, **kwargs)
            except APIError as exc:
                status, reason, _ = describe_exception(exc)
                quota = status == 429 or reason in {"rateLimitExceeded", "RESOURCE_EXHAUSTED"}
                if not last and is_retryable(status, reason) and (idempotent or quota):
                    delay = RETRY_DELAYS[attempt]
                    log.warning("Google Sheets is busy (%s). Retrying in %ss.", status or reason, delay)
                    self._sleep(delay)
                    continue
                raise SheetError(summarize_exception(exc, "sheets")) from exc
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if not last and idempotent:
                    delay = RETRY_DELAYS[attempt]
                    log.warning("Network problem talking to Google Sheets. Retrying in %ss.", delay)
                    self._sleep(delay)
                    continue
                raise SheetError(
                    "Could not reach Google Sheets. Check your internet connection and run it again."
                    + ("" if idempotent else " Re-running import is safe: duplicates are skipped by email.")
                ) from exc
        raise SheetError("Google Sheets did not respond.")  # unreachable; keeps type checkers calm

    def _write_cells(self, data: Sequence[Mapping[str, Any]]) -> None:
        """Batch-write cells. gspread rewrites each item's "range" in place and that rewrite is not
        repeatable, so every attempt (including retries) is handed brand-new dicts."""
        def attempt() -> Any:
            fresh = [{"range": item["range"], "values": [list(r) for r in item["values"]]} for item in data]
            return self._ws.batch_update(fresh, raw=True)

        self._call(attempt)

    # ---- reading
    def snapshot(self) -> Snapshot:
        values = self._call(self._ws.get_all_values)
        if not values or not any(str(c).strip() for c in values[0]):
            raise SheetError(
                f"The '{self._ws.title}' tab is empty (no header row). Run: python main.py init-sheet"
            )
        header = [str(c).strip() for c in values[0]]
        index: dict[str, int] = {}
        for position, name in enumerate(header):
            if not name:
                continue
            if name in index:
                raise SheetError(f"The column '{name}' appears twice in row 1. Delete or rename one of them.")
            index[name] = position
        missing = [c for c in COLUMNS if c not in index]
        if missing:
            raise SheetError(
                "These columns are missing from row 1: " + ", ".join(missing)
                + ". Run: python main.py init-sheet --repair (it adds them without touching your data)."
            )
        rows: list[LeadRow] = []
        for sheet_row, raw in enumerate(values[1:], start=2):
            cells = {name: (str(raw[pos]) if pos < len(raw) else "") for name, pos in index.items()}
            if not (cells[Col.ROW_ID].strip() or cells[Col.COMPANY].strip() or cells[Col.TARGET_EMAIL].strip()):
                continue  # blank or scratch row, not a lead
            rows.append(LeadRow(cells, sheet_row))
        return Snapshot(header, index, rows)

    def read_rows(self) -> list[LeadRow]:
        return self.snapshot().rows

    def get_row(self, row_id: str) -> LeadRow:
        return self._locate(self.snapshot(), row_id)

    @staticmethod
    def _locate(snap: Snapshot, row_id: str) -> LeadRow:
        matches = [r for r in snap.rows if r.row_id == row_id]
        if not matches:
            raise RowNotFound(f"Row {row_id} is no longer in the sheet (deleted?).")
        if len(matches) > 1:
            where = ", ".join(str(r.sheet_row) for r in matches)
            raise SheetError(
                f"Row ID {row_id} appears on sheet rows {where}. Change one of them (or leave the "
                "Row ID blank and it is reassigned automatically)."
            )
        return matches[0]

    def counts(self) -> Counter[tuple[str, str]]:
        """How many rows sit in each (approval, send) state. Blank statuses count as defaults."""
        tally: Counter[tuple[str, str]] = Counter()
        for row in self.read_rows():
            tally[(row.approval or Approval.PENDING, row.send_status or SendStatus.UNSENT)] += 1
        return tally

    # ---- writing
    def update_row(
        self, row_id: str, updates: Mapping[str, str], *, expect: Mapping[str, str] | None = None
    ) -> LeadRow:
        """Write only `updates` to the row with this Row ID.

        `expect` maps column -> required current value. If a human changed any of them since the
        caller read the row, nothing is written and RowChanged is raised.
        """
        snap = self.snapshot()
        unknown = [c for c in list(updates) + list(expect or {}) if c not in snap.index]
        if unknown:
            raise SheetError(f"Unknown column(s) {unknown}: not in the sheet header.")
        row = self._locate(snap, row_id)
        for column, wanted in (expect or {}).items():
            if not same(row.get(column), wanted):
                raise RowChanged(
                    f"{row.label()}: '{column}' is now '{row.get(column)}', not '{wanted}'. Skipped."
                )
        data = [
            {"range": rowcol_to_a1(row.sheet_row, snap.index[column] + 1), "values": [[str(value)]]}
            for column, value in updates.items()
        ]
        if data:
            self._write_cells(data)
        merged = dict(row.values)
        merged.update({column: str(value) for column, value in updates.items()})
        return LeadRow(merged, row.sheet_row)

    def append_leads(self, leads: Iterable[Lead]) -> AppendResult:
        """Add new rows (Pending / Unsent). Skips emails already in the sheet and invalid leads."""
        snap = self.snapshot()
        seen = {r.email.lower() for r in snap.rows if r.email}
        taken = {r.row_id for r in snap.rows if r.row_id}
        result = AppendResult()
        out_rows: list[list[str]] = []
        for lead in leads:
            email = normalize_email(lead.email)
            if not email:
                result.invalid.append((lead, "no valid email address"))
                continue
            if not lead.company.strip():
                result.invalid.append((lead, "no company name"))
                continue
            if email in seen:
                result.duplicates.append(lead)
                continue
            seen.add(email)
            row_id = self._new_row_id(taken)
            taken.add(row_id)
            cells = lead.cell_values()
            cells[Col.TARGET_EMAIL] = email
            cells[Col.CATEGORY] = normalize_category(lead.category) or lead.category.strip()
            cells[Col.ROW_ID] = row_id
            cells[Col.APPROVAL] = Approval.PENDING
            cells[Col.SEND] = SendStatus.UNSENT
            out_rows.append([cells.get(name, "") for name in snap.header])
            result.added_ids.append(row_id)
        if out_rows:
            self._call(self._ws.append_rows, out_rows, idempotent=False,
                       value_input_option=ValueInputOption.raw,
                       insert_data_option=InsertDataOption.insert_rows)
            log.info("Added %d row(s) to the sheet.", len(out_rows))
        return result

    @staticmethod
    def _new_row_id(taken: set[str]) -> str:
        while True:
            candidate = "L" + secrets.token_hex(3).upper()
            if candidate not in taken:
                return candidate

    def prepare_rows(self) -> PrepareResult:
        """Tidy rows a human added by hand: Row ID, default statuses, canonical status spelling.

        Run at the start of draft and send so hand-typed or pasted rows work like imported ones.
        Unrecognised status text (for example 'Aproved') is left alone and reported, and such
        rows are never processed.
        """
        snap = self.snapshot()
        result = PrepareResult()
        taken: set[str] = set()
        used: set[str] = set()
        for row in snap.rows:
            if row.row_id:
                taken.add(row.row_id)
        data: list[dict[str, Any]] = []

        def put(row: LeadRow, column: str, value: str) -> None:
            data.append({"range": rowcol_to_a1(row.sheet_row, snap.index[column] + 1), "values": [[value]]})

        for row in snap.rows:
            row_id = row.row_id
            if not row_id or row_id in used:
                row_id = self._new_row_id(taken | used)
                put(row, Col.ROW_ID, row_id)
                result.ids_assigned += 1
            used.add(row_id)
            for column, allowed, default in (
                (Col.APPROVAL, Approval.ALL, Approval.PENDING),
                (Col.SEND, SendStatus.ALL, SendStatus.UNSENT),
            ):
                current = row.get(column)
                if not current:
                    put(row, column, default)
                    result.defaults_filled += 1
                    continue
                canonical = _canonical(current, allowed)
                if canonical is None:
                    result.unknown_values.append((row.label(), column, current))
                elif canonical != row.values.get(column, ""):
                    put(row, column, canonical)
                    result.canonicalised += 1
        if data:
            self._write_cells(data)
            log.info("Tidied the sheet: %d Row ID(s), %d default status(es), %d spelling fix(es).",
                     result.ids_assigned, result.defaults_filled, result.canonicalised)
        return result

    # ---- layout
    def ensure_layout(self, *, apply_formatting: bool = True) -> list[str]:
        """Create or repair the header row (adds missing columns at the end, never reorders or
        deletes), size the grid, and apply dropdowns and colors. Returns the columns added."""
        values = self._call(self._ws.get_all_values)
        header = [str(c).strip() for c in values[0]] if values else []
        added: list[str] = []
        if not any(header):
            header = list(COLUMNS)
            added = list(COLUMNS)
            self._ensure_grid(len(header))
            self._call(self._ws.update, [header], "A1")
        else:
            added = [c for c in COLUMNS if c not in header]
            if added:
                start = len(header)
                self._ensure_grid(start + len(added))
                self._call(self._ws.update, [added], rowcol_to_a1(1, start + 1))
                header = header + added
            else:
                self._ensure_grid(len(header))
        if apply_formatting:
            self._apply_formatting(header)
        return added

    def _ensure_grid(self, columns_needed: int) -> None:
        rows, cols = int(self._ws.row_count), int(self._ws.col_count)
        if rows < GRID_ROWS or cols < columns_needed:
            self._call(self._ws.resize, rows=max(rows, GRID_ROWS), cols=max(cols, columns_needed))

    def _apply_formatting(self, header: Sequence[str]) -> None:
        index = {name: i for i, name in enumerate(header) if name}
        status_cols = {index[c] for c in (Col.APPROVAL, Col.SEND) if c in index}
        meta = self._call(self._spreadsheet.fetch_sheet_metadata)
        mine = next(
            (s for s in meta.get("sheets", []) if s.get("properties", {}).get("sheetId") == self._ws.id), {}
        )
        stale_cf = [
            position for position, rule in enumerate(mine.get("conditionalFormats", []))
            if any(r.get("startColumnIndex") in status_cols for r in rule.get("ranges", []))
        ]
        stale_protected = [
            p["protectedRangeId"] for p in mine.get("protectedRanges", [])
            if p.get("description") == PROTECTED_DESCRIPTION and "protectedRangeId" in p
        ]
        requests_body = build_layout_requests(
            self._ws.id, header,
            stale_conditional_formats=stale_cf, stale_protected_ranges=stale_protected,
        )
        self._call(self._spreadsheet.batch_update, {"requests": requests_body})
