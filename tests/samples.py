"""Reusable test data: a compliant email, a scripted fake model, and sheet row helpers."""
from __future__ import annotations

from config.settings import COLUMNS, Col
from src.llm import LLMClient, LLMResult

GOOD_SUBJECT = "Penn State Men's Club Volleyball and CeraVe"

GOOD_BODY = (
    "Tournament weekends mean long days outdoors, and skin barrier health and sun protection become part of "
    "the routine for every athlete on our roster.\n\n"
    "Penn State Men's Club Volleyball is a top 10 nationally ranked program in the National Collegiate "
    "Volleyball Federation (NCVF). Our 30 athletes are active across campus in fraternities, cultural "
    "associations, the honors college, and other club sports teams. We are wrapping up our fall tournament "
    "slate and gearing up for our full spring competitive run. We have partnered with brands in the past and "
    "are always excited to showcase and actively promote products our athletes love.\n\n"
    "We would like to talk about a product sponsorship or sending samples for the team to try. Are you the "
    "right person for this, or could you point me to who is?"
)


def model_text(subject: str = GOOD_SUBJECT, body: str = GOOD_BODY) -> str:
    return f"SUBJECT: {subject}\nBODY:\n{body}"


GOOD_MODEL_TEXT = model_text()


class FakeLLM(LLMClient):
    """Scripted model. Each item is a string, an Exception to raise, or a callable(system, prompt)."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []
        self.checked = False

    def generate(self, system: str, prompt: str) -> LLMResult:
        self.calls.append((system, prompt))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(item):
            item = item(system, prompt)
        if isinstance(item, Exception):
            raise item
        return LLMResult(text=str(item), provider=self.provider, model=self.model, input_tokens=100, output_tokens=200)

    def check(self) -> str:
        self.checked = True
        return "fake ok"


def make_row(**cells: str) -> list[str]:
    row = [""] * len(COLUMNS)
    for name, value in cells.items():
        row[COLUMNS.index(name)] = value
    return row


def pending(row_id: str = "L1", company: str = "CeraVe", email: str = "jane@cerave.com", **extra: str) -> list[str]:
    cells = {Col.ROW_ID: row_id, Col.COMPANY: company, Col.CATEGORY: "Skincare", Col.TARGET_NAME: "Jane Doe",
             Col.TARGET_EMAIL: email, Col.APPROVAL: "Pending", Col.SEND: "Unsent"}
    cells.update(extra)
    return make_row(**cells)


def approved(row_id: str = "L1", company: str = "CeraVe", email: str = "jane@cerave.com", **extra: str) -> list[str]:
    cells = {Col.SUBJECT: GOOD_SUBJECT, Col.BODY: "Hi Jane,\n\n" + GOOD_BODY + "\n\nBest,\nVarun Kaveti",
             Col.APPROVAL: "Approved", Col.SEND: "Unsent"}
    cells.update(extra)
    return pending(row_id, company, email, **cells)
