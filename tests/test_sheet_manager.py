import json
import os

import googleapiclient.discovery_cache as discovery_cache
import pytest
import requests

from config.errors import RowChanged, RowNotFound, SheetError
from config.settings import COLUMNS, Approval, Col, SendStatus, Settings
from src.models import Lead
from src.sheet_manager import (
    PROTECTED_DESCRIPTION, SheetManager, build_layout_requests, select_rows, same, tool_note,
)
from tests.fakes import FakeGspreadClient, FakeSpreadsheet, FakeWorksheet, api_error

HEADER = list(COLUMNS)
COL = {name: i + 1 for i, name in enumerate(HEADER)}  # 1-based sheet column of each header


def make_row(**cells):
    row = [""] * len(HEADER)
    for name, value in cells.items():
        row[HEADER.index(name)] = value
    return row


def manager(rows=(), header=None, sleeps=None):
    ws = FakeWorksheet(grid=[header or HEADER] + [list(r) for r in rows])
    ss = FakeSpreadsheet(ws, sheet_id="SHEET123")
    ss.sheet1 = ws
    log = sleeps if sleeps is not None else []
    return SheetManager(ss, ws, sleep=log.append), ws, ss, log


def lead_row(row_id="L1", company="Red Bull", email="a@redbull.com", approval="Pending", send="Unsent", **extra):
    return make_row(**{Col.ROW_ID: row_id, Col.COMPANY: company, Col.TARGET_EMAIL: email,
                       Col.APPROVAL: approval, Col.SEND: send, **extra})


# --------------------------------------------------------------------------- reading
def test_reads_rows_by_header_name_even_if_columns_are_reordered():
    shuffled = list(reversed(HEADER))
    row = [""] * len(shuffled)
    row[shuffled.index(Col.COMPANY)] = "CeraVe"
    row[shuffled.index(Col.ROW_ID)] = "L9"
    m, *_ = manager(rows=[row], header=shuffled)
    rows = m.read_rows()
    assert [(r.row_id, r.company) for r in rows] == [("L9", "CeraVe")]


def test_blank_and_scratch_rows_are_ignored_and_short_rows_are_padded():
    m, *_ = manager(rows=[lead_row("L1"), [""] * len(HEADER), ["", "", "", "", "", "", "", "", "", "", "", "", "", ""],
                          ["L3", "Liquid I.V."]])
    assert [r.row_id for r in m.read_rows()] == ["L1", "L3"]
    assert m.get_row("L3").email == ""


def test_empty_tab_and_missing_or_duplicate_columns_give_clear_errors():
    ws = FakeWorksheet(grid=[])
    with pytest.raises(SheetError, match="init-sheet"):
        SheetManager(FakeSpreadsheet(ws), ws).read_rows()
    short = HEADER[:-2]
    m, *_ = manager(header=short)
    with pytest.raises(SheetError, match="Company Domain.*--repair"):
        m.read_rows()
    dup = HEADER + [Col.COMPANY]
    m, *_ = manager(header=dup)
    with pytest.raises(SheetError, match="appears twice"):
        m.read_rows()


def test_get_row_errors():
    m, *_ = manager(rows=[lead_row("L1"), lead_row("L1", email="b@x.com")])
    with pytest.raises(SheetError, match="appears on sheet rows 2, 3"):
        m.get_row("L1")
    with pytest.raises(RowNotFound):
        m.get_row("NOPE")


def test_counts_treat_blank_status_as_default():
    m, *_ = manager(rows=[lead_row("L1", approval="", send=""), lead_row("L2", approval="Approved"), lead_row("L3", approval="Approved")])
    tally = m.counts()
    assert tally[("Pending", "Unsent")] == 1 and tally[("Approved", "Unsent")] == 2


# --------------------------------------------------------------------------- writing
def test_update_row_writes_only_named_cells_and_keeps_human_edits():
    m, ws, *_ = manager(rows=[lead_row("L1", **{Col.HOOK: "my own angle", Col.BODY: "human edit"})])
    updated = m.update_row("L1", {Col.SUBJECT: "Hello", Col.APPROVAL: Approval.PENDING_APPROVAL})
    assert ws.cell(2, COL[Col.SUBJECT]) == "Hello" and ws.cell(2, COL[Col.APPROVAL]) == "Pending Approval"
    assert ws.cell(2, COL[Col.HOOK]) == "my own angle" and ws.cell(2, COL[Col.BODY]) == "human edit"
    assert updated.subject == "Hello" and updated.row_id == "L1"
    writes = [d for name, d in ws.calls if name == "batch_update"][0]
    assert writes["raw"] is True and len(writes["data"]) == 2


def test_update_finds_the_row_by_id_after_the_sheet_is_sorted():
    m, ws, *_ = manager(rows=[lead_row("L1", company="Red Bull"), lead_row("L2", company="CeraVe", email="c@cerave.com")])
    snap_row = m.get_row("L1")
    assert snap_row.sheet_row == 2
    ws.grid[1], ws.grid[2] = ws.grid[2], ws.grid[1]  # a human sorts the sheet mid-run
    m.update_row("L1", {Col.NOTES: "drafted"})
    assert ws.cell(3, COL[Col.NOTES]) == "drafted" and ws.cell(2, COL[Col.NOTES]) == ""


def test_expect_blocks_the_write_when_a_human_changed_the_row():
    m, ws, *_ = manager(rows=[lead_row("L1", approval="Approved")])
    ws.before_read = lambda w: w._set(2, COL[Col.APPROVAL], "Rejected")
    with pytest.raises(RowChanged, match="Rejected"):
        m.update_row("L1", {Col.SEND: SendStatus.SENDING}, expect={Col.APPROVAL: Approval.APPROVED})
    assert ws.cell(2, COL[Col.SEND]) == "Unsent"
    assert not [c for c in ws.calls if c[0] == "batch_update"]


def test_expect_is_case_and_space_insensitive():
    m, ws, *_ = manager(rows=[lead_row("L1", approval=" approved ")])
    m.update_row("L1", {Col.SEND: "Sending"}, expect={Col.APPROVAL: "Approved"})
    assert ws.cell(2, COL[Col.SEND]) == "Sending"


def test_unknown_column_is_rejected_before_any_write():
    m, ws, *_ = manager(rows=[lead_row("L1")])
    with pytest.raises(SheetError, match="Unknown column"):
        m.update_row("L1", {"Nope": "x"})
    assert not [c for c in ws.calls if c[0] == "batch_update"]


def test_values_are_written_raw_so_formulas_stay_text():
    m, ws, *_ = manager(rows=[lead_row("L1")])
    m.update_row("L1", {Col.BODY: '=HYPERLINK("http://evil","x")'})
    assert [d for n, d in ws.calls if n == "batch_update"][0]["raw"] is True


# --------------------------------------------------------------------------- retries
def test_quota_errors_are_retried_with_a_rebuilt_request():
    m, ws, _, sleeps = manager(rows=[lead_row("L1")])
    ws.failures["batch_update"] = [api_error(429, "Quota exceeded", "RESOURCE_EXHAUSTED"), api_error(429, "Quota exceeded")]
    m.update_row("L1", {Col.NOTES: "ok"})
    assert ws.cell(2, COL[Col.NOTES]) == "ok"
    assert sleeps == [2, 5]  # two waits, then success: the retry sent a clean A1 range each time


def test_server_errors_retry_for_reads_but_a_hard_error_raises_a_friendly_message():
    m, ws, _, sleeps = manager(rows=[lead_row("L1")])
    ws.failures["get_all_values"] = [api_error(503)]
    assert m.read_rows()[0].row_id == "L1" and sleeps == [2]

    m2, ws2, *_ = manager(rows=[lead_row("L1")])
    ws2.failures["get_all_values"] = [api_error(403, "API not enabled", "PERMISSION_DENIED")]
    with pytest.raises(SheetError):
        m2.read_rows()


def test_retries_give_up_with_a_clear_message():
    m, ws, _, sleeps = manager(rows=[lead_row("L1")])
    ws.failures["get_all_values"] = [api_error(429)] * 10
    with pytest.raises(SheetError, match="rate limiting"):
        m.read_rows()
    assert len(sleeps) == 5


def test_network_errors_retry_on_reads_but_never_on_append():
    m, ws, _, sleeps = manager(rows=[lead_row("L1")])
    ws.failures["get_all_values"] = [requests.exceptions.ConnectionError("down")]
    assert m.read_rows() and sleeps == [2]

    m2, ws2, _, sleeps2 = manager(rows=[])
    ws2.failures["append_rows"] = [requests.exceptions.ConnectionError("down")]
    with pytest.raises(SheetError, match="duplicates are skipped"):
        m2.append_leads([Lead(company="X", email="x@x.com")])
    assert sleeps2 == []  # an append is not re-sent blindly: it could double-add rows


def test_append_still_retries_quota_errors():
    m, ws, _, sleeps = manager(rows=[])
    ws.failures["append_rows"] = [api_error(429, "Quota exceeded", "RESOURCE_EXHAUSTED")]
    result = m.append_leads([Lead(company="X", email="x@x.com")])
    assert len(result.added_ids) == 1 and sleeps == [2]


# --------------------------------------------------------------------------- append
def test_append_leads_adds_defaults_dedupes_and_reports_invalid():
    m, ws, *_ = manager(rows=[lead_row("L1", email="old@redbull.com")])
    result = m.append_leads([
        Lead(company="CeraVe", email="  Jane@CeraVe.com ", category="skin care", target_name="Jane", domain="cerave.com"),
        Lead(company="Red Bull", email="OLD@redbull.com"),            # already in the sheet
        Lead(company="Dup", email="jane@cerave.com"),                  # duplicate within the batch
        Lead(company="NoMail", email="not-an-email"),
        Lead(company="", email="x@y.com"),
    ])
    assert len(result.added_ids) == 1 and len(result.duplicates) == 2 and len(result.invalid) == 2
    new_id = result.added_ids[0]
    row = m.get_row(new_id)
    assert row.email == "jane@cerave.com" and row.category == "Skincare" and row.domain == "cerave.com"
    assert (row.approval, row.send_status) == ("Pending", "Unsent")
    call = [d for n, d in ws.calls if n == "append_rows"][0]
    assert str(call["value_input_option"]).endswith("RAW") or call["value_input_option"] == "RAW"
    assert "INSERT_ROWS" in str(call["insert_data_option"]).upper()


def test_append_with_nothing_new_makes_no_api_write():
    m, ws, *_ = manager(rows=[lead_row("L1", email="a@b.com")])
    assert m.append_leads([Lead(company="A", email="a@b.com")]).added_ids == []
    assert not [c for c in ws.calls if c[0] == "append_rows"]


def test_new_row_ids_are_unique():
    m, *_ = manager(rows=[])
    result = m.append_leads([Lead(company=f"C{i}", email=f"p{i}@c{i}.com") for i in range(50)])
    assert len(set(result.added_ids)) == 50 and all(i.startswith("L") for i in result.added_ids)


# --------------------------------------------------------------------------- prepare_rows
def test_prepare_assigns_ids_defaults_and_fixes_spelling():
    m, ws, *_ = manager(rows=[
        make_row(**{Col.COMPANY: "Hand Typed", Col.TARGET_EMAIL: "h@t.com"}),                 # no id, no statuses
        lead_row("L2", approval="approved", send="UNSENT"),                                    # wrong case
        lead_row("L2", email="dup@x.com", approval="Pending"),                                 # duplicate id
        lead_row("L4", approval="Aproved"),                                                    # typo: left alone
    ])
    result = m.prepare_rows()
    assert result.ids_assigned == 2 and result.defaults_filled == 2 and result.canonicalised == 2
    assert len(result.unknown_values) == 1 and "Aproved" in result.unknown_values[0][2]
    rows = m.read_rows()
    assert len({r.row_id for r in rows}) == 4 and all(r.row_id for r in rows)
    assert rows[0].approval == "Pending" and rows[0].send_status == "Unsent"
    assert rows[1].approval == "Approved" and rows[1].send_status == "Unsent"
    assert rows[3].approval == "Aproved"


def test_prepare_on_a_clean_sheet_writes_nothing():
    m, ws, *_ = manager(rows=[lead_row("L1"), lead_row("L2", email="b@b.com", approval="Approved")])
    assert m.prepare_rows().changed == 0
    assert not [c for c in ws.calls if c[0] == "batch_update"]


# --------------------------------------------------------------------------- select/same/notes
def test_select_rows_and_comparison_helpers():
    m, *_ = manager(rows=[
        lead_row("L1", approval="Approved", send="Unsent"), lead_row("L2", approval="Approved", send="Drafted"),
        lead_row("L3", approval="Pending", send="Unsent"), lead_row("L4", approval=" approved ", send="unsent"),
    ])
    rows = m.read_rows()
    assert [r.row_id for r in select_rows(rows, approval=Approval.APPROVED, send=SendStatus.UNSENT)] == ["L1", "L4"]
    assert [r.row_id for r in select_rows(rows, approval=Approval.PENDING)] == ["L3"]
    assert same(" Approved", "approved") and not same("Approved", "Rejected") and same(None, "")


def test_tool_note_replaces_its_own_line_and_keeps_human_notes():
    note = tool_note("call them first\n[draft] old problem", "draft", "new problem")
    assert note == "call them first\n[draft] new problem"
    assert tool_note(note, "draft", "") == "call them first"
    assert tool_note("", "send", "boom") == "[send] boom"


# --------------------------------------------------------------------------- connect / create / layout
def settings(**env):
    return Settings.load(env={"SPREADSHEET_ID": "SHEET123", **env})


def test_connect_success_and_friendly_failures():
    ws = FakeWorksheet(grid=[HEADER])
    client = FakeGspreadClient(FakeSpreadsheet(ws))
    assert SheetManager.connect(settings(), client).spreadsheet_id == "SHEET123"
    with pytest.raises(SheetError, match="could not find that spreadsheet"):
        SheetManager.connect(settings(SPREADSHEET_ID="WRONG"), client)
    with pytest.raises(SheetError, match="Fix WORKSHEET_NAME"):
        SheetManager.connect(settings(WORKSHEET_NAME="Other"), client)
    with pytest.raises(Exception, match="SPREADSHEET_ID is empty"):
        SheetManager.connect(Settings.load(env={}), client)


def test_create_new_builds_the_sheet_and_shares_it():
    client = FakeGspreadClient()
    m = SheetManager.create_new(settings(), client, share_with=["me@psu.edu"], sleep=lambda s: None)
    ws = client.spreadsheet.sheet1
    assert ws.title == "Outreach Queue" and ws.grid[0] == HEADER
    assert client.spreadsheet.shared_with == ["me@psu.edu"]
    assert client.spreadsheet.batch_bodies and m.url.endswith("/SHEET123")
    assert ws.row_count >= 1000 and ws.col_count >= len(HEADER)


def test_share_failure_does_not_lose_the_new_sheet():
    client = FakeGspreadClient()
    orig_create = client.create

    def create_with_bad_share(title):
        ss = orig_create(title)
        ss.share_error = api_error(400, "invalid email")
        return ss

    client.create = create_with_bad_share
    m = SheetManager.create_new(settings(), client, share_with=["bad address"], sleep=lambda s: None)
    assert m.spreadsheet_id == "SHEET123"


def test_ensure_layout_repairs_missing_columns_without_touching_data():
    short = HEADER[:-3]
    row = [""] * len(short)
    row[short.index(Col.COMPANY)] = "Keep Me"
    m, ws, *_ = manager(rows=[row], header=short)
    added = m.ensure_layout()
    assert added == HEADER[-3:]
    assert ws.grid[0] == short + HEADER[-3:] and ws.grid[1][short.index(Col.COMPANY)] == "Keep Me"
    assert m.read_rows()[0].company == "Keep Me"


def test_ensure_layout_on_complete_sheet_adds_nothing_and_reruns_cleanly():
    m, ws, ss, _ = manager(rows=[lead_row("L1")])
    assert m.ensure_layout() == [] and m.ensure_layout() == []
    assert len(ss.batch_bodies) == 2


def test_ensure_layout_removes_stale_rules_so_reruns_do_not_stack_them():
    m, ws, ss, _ = manager()
    approval_col = HEADER.index(Col.APPROVAL)
    ss.metadata = {"sheets": [{
        "properties": {"sheetId": ws.id},
        "conditionalFormats": [
            {"ranges": [{"startColumnIndex": approval_col}]},
            {"ranges": [{"startColumnIndex": 0}]},                      # someone else's rule: keep
        ],
        "protectedRanges": [
            {"protectedRangeId": 5, "description": PROTECTED_DESCRIPTION},
            {"protectedRangeId": 6, "description": "not ours"},
        ],
    }]}
    m.ensure_layout()
    kinds = [next(iter(r)) for r in ss.batch_bodies[-1]["requests"]]
    deletes = [r for r in ss.batch_bodies[-1]["requests"] if "deleteConditionalFormatRule" in r]
    assert [d["deleteConditionalFormatRule"]["index"] for d in deletes] == [0]
    assert [r["deleteProtectedRange"]["protectedRangeId"] for r in ss.batch_bodies[-1]["requests"] if "deleteProtectedRange" in r] == [5]
    assert kinds.index("deleteConditionalFormatRule") < kinds.index("addConditionalFormatRule")


# --------------------------------------------------------------------------- layout checked against Google's schema
@pytest.fixture(scope="module")
def sheets_schema():
    path = os.path.join(os.path.dirname(discovery_cache.__file__), "documents", "sheets.v4.json")
    with open(path) as handle:
        return json.load(handle)["schemas"]


def _check(value, schema, schemas, where):
    """Tiny JSON-schema walker for Google discovery documents: unknown field names and bad enums fail."""
    if "$ref" in schema:
        return _check(value, schemas[schema["$ref"]], schemas, where)
    if schema.get("type") == "array":
        assert isinstance(value, list), f"{where}: expected list"
        for i, item in enumerate(value):
            _check(item, schema["items"], schemas, f"{where}[{i}]")
        return
    if "properties" in schema or schema.get("type") == "object":
        assert isinstance(value, dict), f"{where}: expected object"
        props = schema.get("properties", {})
        for key, sub in value.items():
            assert key in props, f"{where}: '{key}' is not a field of this Sheets type"
            _check(sub, props[key], schemas, f"{where}.{key}")
        return
    if "enum" in schema:
        assert value in schema["enum"], f"{where}: {value!r} not in {schema['enum']}"


def test_every_layout_request_matches_the_sheets_api_schema(sheets_schema):
    reqs = build_layout_requests(7, HEADER, stale_conditional_formats=[1, 0], stale_protected_ranges=[9])
    request_schema = sheets_schema["Request"]["properties"]
    for i, request in enumerate(reqs):
        assert len(request) == 1
        (name, body), = request.items()
        assert name in request_schema, f"{name} is not a Sheets request type"
        _check(body, request_schema[name], sheets_schema, f"requests[{i}].{name}")


def test_layout_covers_the_columns_that_matter():
    reqs = build_layout_requests(7, HEADER)
    validations = [r["setDataValidation"] for r in reqs if "setDataValidation" in r]
    assert len(validations) == 3
    by_col = {v["range"]["startColumnIndex"]: v["rule"] for v in validations}
    approval = by_col[HEADER.index(Col.APPROVAL)]
    assert [v["userEnteredValue"] for v in approval["condition"]["values"]] == Approval.ALL and approval["strict"] is True
    assert by_col[HEADER.index(Col.CATEGORY)]["strict"] is False
    assert sum("addConditionalFormatRule" in r for r in reqs) == 8
    assert any(r.get("updateSheetProperties", {}).get("properties", {}).get("gridProperties", {}).get("frozenRowCount") == 1 for r in reqs)
    widths = {r["updateDimensionProperties"]["range"]["startIndex"] for r in reqs if "updateDimensionProperties" in r}
    assert widths == set(range(len(HEADER)))
