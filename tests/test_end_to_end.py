"""The whole pipeline, start to finish, against fakes: import -> draft -> human approves -> drafts -> live send.

This is the rehearsal for the real thing. It follows the exact path the README tells you to take.
"""
import base64
from email import message_from_bytes, policy

import pytest

from config.settings import Col
from src import discovery as D
from tests.harness import COL, Harness
from tests.samples import GOOD_MODEL_TEXT, FakeLLM, model_text

CONTACTS = (
    "Company Name,Target Email,Target Name,Category,Custom Hook / Angle\n"
    "CeraVe,jane.doe@cerave.com,Jane Doe,Skincare,\n"
    "Liquid I.V.,sam@liquid-iv.com,Sam Lee,Hydration,\n"
    "Red Bull,pat@redbull.com,,Energy,their new summer can\n"
    "Celsius,x@celsius.com,,Energy,\n"
)


def decode(raw):
    return message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def approve(h, *row_ids):
    for row_id in row_ids:
        row = h.sheet.get_row(row_id)
        h.ws._set(row.sheet_row, COL[Col.APPROVAL], "Approved")


@pytest.fixture
def csv_file(tmp_path):
    path = tmp_path / "contacts.csv"
    path.write_text(CONTACTS)
    return path


def test_the_full_workflow(tmp_path, csv_file):
    h = Harness(tmp_path)

    # 1. contacts go in as Pending / Unsent; the banned brand never gets a row
    assert h.run("import", "--csv", str(csv_file)) == 0
    rows = h.sheet.read_rows()
    assert [r.company for r in rows] == ["CeraVe", "Liquid I.V.", "Red Bull"]
    assert {(r.approval, r.send_status) for r in rows} == {("Pending", "Unsent")}

    # 2. the model drafts; every row waits for a human
    assert h.run("draft") == 0
    rows = h.sheet.read_rows()
    assert {r.approval for r in rows} == {"Pending Approval"} and all(r.subject and r.body for r in rows)
    assert h.llm.calls and "Celsius" not in h.llm.calls[0][0]
    assert "their new summer can" in next(p for _, p in h.llm.calls if "Red Bull" in p)  # the custom angle reached the model
    assert h.gmail.drafts == {} and h.gmail.sent == []  # nothing has left the building

    # 3. sending before approval does nothing
    h.lines.clear()
    assert h.run("send") == 0 and "Nothing to send" in h.text and h.gmail.drafts == {}

    # 4. the human edits one email and approves two, rejects one
    cera, liquid, bull = (r.row_id for r in h.sheet.read_rows())
    cera_row = h.sheet.get_row(cera)
    h.ws._set(cera_row.sheet_row, COL[Col.SUBJECT], "A subject Varun wrote himself")
    approve(h, cera, liquid)
    h.ws._set(h.sheet.get_row(bull).sheet_row, COL[Col.APPROVAL], "Rejected")
    assert h.run("status") == 0 and "approved, ready to send" in h.text and "rejected" in h.text

    # 5. default send creates Gmail drafts only
    assert h.run("send") == 0
    assert len(h.gmail.drafts) == 2 and h.gmail.sent == []
    drafted = {decode(raw)["To"]: decode(raw) for raw in h.gmail.drafts.values()}
    assert set(drafted) == {"jane.doe@cerave.com", "sam@liquid-iv.com"}
    msg = drafted["jane.doe@cerave.com"]
    assert msg["Subject"] == "A subject Varun wrote himself" and msg["From"] == "Varun Kaveti <pennstatemcvb@gmail.com>"
    body = msg.get_content()
    assert body.startswith("Hi Jane,") and "@psumensclubvolleyball" in body and body.rstrip().endswith("Personal Instagram: @varunkaveti")
    assert "We have worked with brands before and it has always been pretty informal, just getting product in the hands of athletes who are genuinely using it." in body
    states = {r.company: (r.approval, r.send_status) for r in h.sheet.read_rows()}
    assert states == {"CeraVe": ("Approved", "Drafted"), "Liquid I.V.": ("Approved", "Drafted"), "Red Bull": ("Rejected", "Unsent")}

    # 6. the human sends one draft by hand in Gmail; sync notices
    sent_by_hand = next(i for i, raw in h.gmail.drafts.items() if decode(raw)["To"] == "jane.doe@cerave.com")
    del h.gmail.drafts[sent_by_hand]
    h.lines.clear()
    assert h.run("sync") == 0 and "1 left Gmail Drafts" in h.text
    assert {r.company: r.send_status for r in h.sheet.read_rows()}["CeraVe"] == "Sent"

    # 7. a second send creates nothing new: Drafted and Sent rows are never reprocessed
    before = len(h.gmail.drafts)
    assert h.run("send") == 0 and len(h.gmail.drafts) == before and "Nothing to send" in h.text


def test_live_send_path_end_to_end(tmp_path, csv_file):
    h = Harness(tmp_path)
    h.run("import", "--csv", str(csv_file))
    h.run("draft")
    approve(h, *(r.row_id for r in h.sheet.read_rows()))
    h.lines.clear()
    assert h.run("send", "--live", "--yes") == 0
    assert len(h.gmail.sent) == 3 and h.gmail.drafts == {}
    assert len(h.sleeps) == 2 and all(180 <= s <= 420 for s in h.sleeps)
    rows = h.sheet.read_rows()
    assert {r.send_status for r in rows} == {"Sent"} and all(r.get(Col.SENT_AT).startswith("2026-10-18T15:30:00") for r in rows)
    assert sorted(decode(raw)["To"] for raw in h.gmail.sent) == ["jane.doe@cerave.com", "pat@redbull.com", "sam@liquid-iv.com"]

    h.lines.clear()
    assert h.run("send", "--live", "--yes") == 0 and len(h.gmail.sent) == 3  # running it again sends nothing


def test_the_fifteen_per_run_cap_across_two_runs(tmp_path):
    lines = ["Company Name,Target Email,Category"] + [f"Brand{i},person{i}@brand{i}.com,Gear" for i in range(1, 21)]
    path = tmp_path / "twenty.csv"
    path.write_text("\n".join(lines) + "\n")
    h = Harness(tmp_path)
    h.llm = h.rt._llm = FakeLLM(GOOD_MODEL_TEXT)
    h.run("import", "--csv", str(path))
    h.run("draft", "--limit", "25")
    approve(h, *(r.row_id for r in h.sheet.read_rows()))
    assert h.run("send", "--live", "--yes") == 0 and len(h.gmail.sent) == 15 and "5 approved rows left for the next run" in h.text
    assert h.run("send", "--live", "--yes") == 0 and len(h.gmail.sent) == 20
    assert len({decode(raw)["To"] for raw in h.gmail.sent}) == 20


def test_a_draft_that_breaks_the_rules_is_held_back_and_explained(tmp_path):
    path = tmp_path / "one.csv"
    path.write_text("Company Name,Target Email,Category\nCeraVe,jane@cerave.com,Skincare\n")
    h = Harness(tmp_path)
    h.rt._llm = FakeLLM(model_text(body="Short and wrong. Celsius is great."))
    h.run("import", "--csv", str(path))
    assert h.run("draft") == 0 and "FAILED" in h.text
    row = h.sheet.read_rows()[0]
    assert row.approval == "Pending" and row.body == "" and "draft rejected" in row.notes and "Celsius" in row.notes
    approve(h, row.row_id)  # even if a human approves an empty row by mistake, it cannot go out
    h.lines.clear()
    assert h.run("send", "--live", "--yes") == 0 and h.gmail.sent == [] and "BLOCKED" in h.text
    assert "subject is empty" in h.sheet.read_rows()[0].notes


def test_discover_then_draft_then_send(tmp_path):
    path = tmp_path / "brands.csv"
    path.write_text("Company Name,Domain,Category\nNuun,nuunlife.com,Hydration\n")
    h = Harness(tmp_path)

    class Backend:
        name = "Stub"

        def find_contacts(self, target, **kw):
            return D.LookupResult([D.Contact("ana@nuunlife.com", "Ana", "Ruiz", "Partnerships Manager", 95, "valid", "Stub")], "nuunlife.com", "Nuun", 1)

    h.rt._backend = Backend()
    assert h.run("discover", "--csv", str(path)) == 0
    row = h.sheet.read_rows()[0]
    assert (row.company, row.email, row.target_name, row.domain, row.category) == ("Nuun", "ana@nuunlife.com", "Ana Ruiz", "nuunlife.com", "Hydration")
    assert h.run("draft") == 0 and h.sheet.read_rows()[0].body.startswith("Hi Ana,")
    approve(h, row.row_id)
    assert h.run("send") == 0 and h.sheet.read_rows()[0].send_status == "Drafted"
    h.lines.clear()
    assert h.run("discover", "--csv", str(path)) == 0 and "nuunlife.com is already in the sheet" in h.text  # no credits spent twice
