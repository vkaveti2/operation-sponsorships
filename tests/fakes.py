"""In-memory stand-ins for gspread objects, so no test ever touches the network."""
from __future__ import annotations

from typing import Any, Callable

from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from gspread.utils import a1_to_rowcol, absolute_range_name


class FakeResponse:
    def __init__(self, code: int, message: str = "boom", status: str = "ERROR", reason: str = ""):
        self.status_code = code
        err: dict[str, Any] = {"code": code, "message": message, "status": status}
        if reason:
            err["errors"] = [{"reason": reason}]
        self._payload = {"error": err}
        self.text = str(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def api_error(code: int, message: str = "boom", status: str = "ERROR", reason: str = "") -> APIError:
    return APIError(FakeResponse(code, message, status, reason))


class FakeWorksheet:
    """Behaves like gspread.Worksheet for the calls SheetManager makes."""

    def __init__(self, title: str = "Outreach Queue", grid: list[list[str]] | None = None,
                 sheet_id: int = 11, rows: int = 1000, cols: int = 26):
        self.title = title
        self.id = sheet_id
        self.grid: list[list[str]] = [list(r) for r in (grid or [])]
        self._rows, self._cols = rows, cols
        self.calls: list[tuple[str, Any]] = []
        self.failures: dict[str, list[Exception]] = {}  # method name -> exceptions to raise, in order
        self.before_read: Callable[["FakeWorksheet"], None] | None = None  # simulate a human editing

    # ---- properties gspread exposes
    @property
    def row_count(self) -> int:
        return self._rows

    @property
    def col_count(self) -> int:
        return self._cols

    def _enter(self, name: str, detail: Any = None) -> None:
        self.calls.append((name, detail))
        queue = self.failures.get(name)
        if queue:
            raise queue.pop(0)

    # ---- grid helpers
    def _set(self, row: int, col: int, value: str) -> None:
        while len(self.grid) < row:
            self.grid.append([])
        line = self.grid[row - 1]
        line.extend([""] * (col - len(line)))
        line[col - 1] = value

    def cell(self, row: int, col: int) -> str:
        if row > len(self.grid) or col > len(self.grid[row - 1]):
            return ""
        return self.grid[row - 1][col - 1]

    @staticmethod
    def _parse(range_name: str) -> tuple[int, int]:
        ref = range_name.split("!")[-1].split(":")[0]
        return a1_to_rowcol(ref)

    # ---- gspread surface
    def get_all_values(self, *a: Any, **kw: Any) -> list[list[str]]:
        self._enter("get_all_values")
        if self.before_read:
            hook, self.before_read = self.before_read, None
            hook(self)
        rows = [list(r) for r in self.grid]
        while rows and not any(str(c).strip() for c in rows[-1]):
            rows.pop()
        width = max((len(r) for r in rows), default=0)
        return [r + [""] * (width - len(r)) for r in rows]

    def batch_update(self, data: Any, raw: bool = True, **kw: Any) -> dict[str, Any]:
        data = list(data)
        # Real gspread rewrites each caller dict's "range" in place and is not idempotent about it.
        for item in data:
            item["range"] = absolute_range_name(self.title, item["range"])
        self._enter("batch_update", {"data": [dict(d) for d in data], "raw": raw})
        for item in data:
            if item["range"].count("!") != 1:  # what Google answers with a 400
                raise api_error(400, f"Unable to parse range: {item['range']}", "INVALID_ARGUMENT")
            row, col = self._parse(item["range"])
            for r_off, values in enumerate(item["values"]):
                for c_off, value in enumerate(values):
                    self._set(row + r_off, col + c_off, str(value))
        return {}

    def update(self, values: Any, range_name: str | None = None, raw: bool = True, **kw: Any) -> dict[str, Any]:
        self._enter("update", {"range": range_name, "values": values})
        row, col = self._parse(range_name or "A1")
        for r_off, line in enumerate(values):
            for c_off, value in enumerate(line):
                self._set(row + r_off, col + c_off, str(value))
        return {}

    def append_rows(self, values: Any, value_input_option: Any = None, insert_data_option: Any = None, **kw: Any) -> dict[str, Any]:
        self._enter("append_rows", {"value_input_option": value_input_option, "insert_data_option": insert_data_option})
        used = len(self.get_all_values())
        for offset, line in enumerate(values):
            for c, value in enumerate(line, start=1):
                self._set(used + 1 + offset, c, str(value))
        return {}

    def resize(self, rows: int | None = None, cols: int | None = None) -> dict[str, Any]:
        self._enter("resize", (rows, cols))
        self._rows = rows if rows is not None else self._rows
        self._cols = cols if cols is not None else self._cols
        return {}

    def update_title(self, title: str) -> dict[str, Any]:
        self._enter("update_title", title)
        self.title = title
        return {}


class FakeSpreadsheet:
    def __init__(self, worksheet: FakeWorksheet | None = None, title: str = "Fake Sheet", sheet_id: str = "SHEET123"):
        self.sheet1 = worksheet or FakeWorksheet(title="Sheet1")
        self.title = title
        self.id = sheet_id
        self.url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        self.batch_bodies: list[dict[str, Any]] = []
        self.shared_with: list[str] = []
        self.metadata: dict[str, Any] = {"sheets": []}
        self.share_error: Exception | None = None

    def worksheets(self) -> list[FakeWorksheet]:
        return [self.sheet1]

    def worksheet(self, title: str) -> FakeWorksheet:
        if title == self.sheet1.title:
            return self.sheet1
        raise WorksheetNotFound(title)

    def fetch_sheet_metadata(self, params: Any = None) -> dict[str, Any]:
        return self.metadata

    def batch_update(self, body: dict[str, Any]) -> dict[str, Any]:
        self.batch_bodies.append(body)
        return {}

    def share(self, email: str, perm_type: str, role: str, notify: bool = True, email_message: str | None = None, **kw: Any) -> None:
        if self.share_error:
            raise self.share_error
        self.shared_with.append(email)


class FakeGspreadClient:
    def __init__(self, spreadsheet: FakeSpreadsheet | None = None):
        self.spreadsheet = spreadsheet
        self.created_titles: list[str] = []

    def open_by_key(self, key: str) -> FakeSpreadsheet:
        if self.spreadsheet is None or key != self.spreadsheet.id:
            raise SpreadsheetNotFound(FakeResponse(404))
        return self.spreadsheet

    def create(self, title: str) -> FakeSpreadsheet:
        self.created_titles.append(title)
        self.spreadsheet = FakeSpreadsheet(title=title)
        return self.spreadsheet


class FakeGmail:
    """Stands in for GmailApi. Scripts are lists consumed one call at a time: None = succeed, exception = raise."""

    def __init__(self, email="pennstatemcvb@gmail.com"):
        self.email = email
        self.sent, self.drafts, self.calls = [], {}, []
        self.send_script, self.draft_script, self.exists_script = [], [], []
        self.before_send = None

    @staticmethod
    def _next(script):
        if script:
            item = script.pop(0)
            if item is not None:
                raise item

    def profile_email(self):
        self.calls.append("profile")
        return self.email

    def create_draft(self, raw):
        self.calls.append("create_draft")
        self._next(self.draft_script)
        draft_id = f"d{len(self.drafts) + 1}"
        self.drafts[draft_id] = raw
        return draft_id

    def send_raw(self, raw):
        self.calls.append("send")
        if self.before_send:
            self.before_send(raw)
        self._next(self.send_script)
        self.sent.append(raw)
        return f"m{len(self.sent)}"

    def draft_exists(self, draft_id):
        self.calls.append("exists")
        self._next(self.exists_script)
        return draft_id in self.drafts
