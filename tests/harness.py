"""Shared test harness: a CLI Runtime wired to in-memory fakes, with everything it prints captured."""
from __future__ import annotations

from datetime import date, datetime, timezone

from config.settings import COLUMNS, Settings
from src.cli import Runtime, main
from src.sheet_manager import SheetManager
from tests.fakes import FakeGmail, FakeSpreadsheet, FakeWorksheet
from tests.samples import GOOD_MODEL_TEXT, FakeLLM

NOW = datetime(2026, 10, 18, 15, 30, tzinfo=timezone.utc)
COL = {name: i + 1 for i, name in enumerate(COLUMNS)}


class Harness:
    """A Runtime wired to fakes, with everything it prints captured in `.lines`."""

    def __init__(self, tmp_path, rows=(), env=None, **runtime_kwargs):
        self.lines: list[str] = []
        self.sleeps: list[float] = []
        self.ws = FakeWorksheet(grid=[list(COLUMNS)] + [list(r) for r in rows])
        self.sheet = SheetManager(FakeSpreadsheet(self.ws), self.ws, sleep=lambda s: None)
        self.gmail = FakeGmail()
        self.llm = FakeLLM(GOOD_MODEL_TEXT)
        self.env_path = tmp_path / ".env"
        self.lookups_path = tmp_path / "lookups.json"
        self.settings = Settings.load(env=env if env is not None else {"SPREADSHEET_ID": "SHEET123"})
        kwargs = dict(out=self.lines.append, interactive=False, today=date(2026, 10, 18), env_path=self.env_path, lookups_path=tmp_path / "lookups.json",
                      sheet=self.sheet, gmail=self.gmail, llm=self.llm, sleep=self.sleeps.append, clock=lambda: NOW)
        kwargs.update(runtime_kwargs)
        self.rt = Runtime(self.settings, **kwargs)

    def run(self, *argv):
        return main(list(argv), runtime=self.rt)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)
