import base64
import dataclasses
import json
import logging
import random
import socket
import socketserver
import threading
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests
from google.auth.exceptions import RefreshError

from config.errors import MailError, SheetError
from config.settings import COLUMNS, HARD_CAP_PER_RUN, Col, Sender, Settings
from src import mailer as M
from src.sheet_manager import SheetManager
from tests.fakes import FakeGmail, FakeSpreadsheet, FakeWorksheet
from tests.samples import GOOD_BODY, approved

NOW = datetime(2026, 10, 18, 15, 30, tzinfo=timezone.utc)
COL = {name: i + 1 for i, name in enumerate(COLUMNS)}


def decode(raw):
    return message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def build(rows, gmail=None, env=None, seed=1, rng=None):
    ws = FakeWorksheet(grid=[list(COLUMNS)] + [list(r) for r in rows])
    sheet = SheetManager(FakeSpreadsheet(ws), ws, sleep=lambda s: None)
    gmail = gmail or FakeGmail()
    sleeps = []
    mailer = M.Mailer(Settings.load(env=env or {}), sheet, gmail, sleep=sleeps.append,
                      rng=rng or random.Random(seed), clock=lambda: NOW)
    return mailer, sheet, ws, gmail, sleeps


def approved_rows(n, start=1):
    return [approved(f"L{i}", f"Brand{i}", f"p{i}@brand{i}.com") for i in range(start, start + n)]


def cell(ws, row, column):
    return ws.cell(row, COL[column])


def statuses(sheet):
    return {r.row_id: (r.approval, r.send_status) for r in sheet.read_rows()}


def call(kind, text="boom", status=0):
    return M.GmailCallError(kind, text, status)


# --------------------------------------------------------------------------- draft mode (the default)
def test_draft_mode_creates_gmail_drafts_and_never_sends():
    mailer, sheet, ws, gmail, sleeps = build(approved_rows(3))
    report = mailer.run()
    assert [o.status for o in report.outcomes] == ["drafted"] * 3 and not report.live
    assert gmail.sent == [] and "send" not in gmail.calls and len(gmail.drafts) == 3
    rows = sheet.read_rows()
    assert [r.send_status for r in rows] == ["Drafted"] * 3 and [r.get(Col.GMAIL_ID) for r in rows] == ["d1", "d2", "d3"]
    assert all(r.approval == "Approved" for r in rows) and all(r.get(Col.SENT_AT) == "" for r in rows)
    assert sleeps == []  # nothing is sent, so no pause is needed


def test_the_draft_carries_the_text_in_the_sheet_including_human_edits():
    rows = [approved("L1", "CeraVe", "Jane@CeraVe.com", **{Col.SUBJECT: "A human edited this subject",
                                                           Col.BODY: "Hi Jane,\n\n" + GOOD_BODY + "\n\nHuman added line.\n\nBest,\nVarun Kaveti"})]
    mailer, sheet, ws, gmail, _ = build(rows)
    mailer.run()
    msg = decode(gmail.drafts["d1"])
    assert msg["From"] == "Varun Kaveti <pennstatemcvb@gmail.com>" and msg["To"] == "jane@cerave.com"
    assert msg["Subject"] == "A human edited this subject"
    assert "Human added line." in msg.get_content() and msg.get_content().startswith("Hi Jane,")


@pytest.mark.parametrize("approval,send", [
    ("Pending", "Unsent"), ("Pending Approval", "Unsent"), ("Rejected", "Unsent"),
    ("Approved", "Sent"), ("Approved", "Drafted"), ("Approved", "Failed"), ("Approved", "Sending"),
])
def test_only_approved_and_unsent_rows_are_ever_touched(approval, send):
    mailer, sheet, ws, gmail, _ = build([approved("L1", **{Col.APPROVAL: approval, Col.SEND: send}), approved("L2", email="other@x.com")])
    report = mailer.run(live=True)
    assert [o.row_id for o in report.outcomes] == ["L2"] and len(gmail.sent) == 1
    assert statuses(sheet)["L1"] == (approval, send)


def test_hand_added_row_with_odd_case_and_blank_send_status_is_picked_up():
    row = approved("", "Liquid I.V.", "pat@liquid-iv.com", **{Col.APPROVAL: "approved", Col.SEND: "", Col.ROW_ID: ""})
    mailer, sheet, ws, gmail, _ = build([row])
    report = mailer.run(live=True)
    assert len(report.sent) == 1 and sheet.read_rows()[0].row_id.startswith("L")


def test_a_misspelled_status_is_never_processed_and_is_logged(caplog):
    mailer, sheet, ws, gmail, _ = build([approved("L1", **{Col.APPROVAL: "Aproved"})])
    with caplog.at_level(logging.WARNING):
        report = mailer.run(live=True)
    assert report.outcomes == [] and gmail.sent == [] and "Aproved" in caplog.text


# --------------------------------------------------------------------------- live mode
def test_live_send_records_sent_timestamp_and_gmail_id():
    mailer, sheet, ws, gmail, _ = build(approved_rows(1))
    report = mailer.run(live=True)
    assert report.live and [o.status for o in report.outcomes] == ["sent"] and len(gmail.sent) == 1
    row = sheet.read_rows()[0]
    assert (row.approval, row.send_status) == ("Approved", "Sent")
    assert row.get(Col.SENT_AT) == "2026-10-18T15:30:00+00:00" and row.get(Col.GMAIL_ID) == "m1"
    assert decode(gmail.sent[0])["To"] == "p1@brand1.com"


def test_the_row_is_marked_sending_before_gmail_is_called():
    seen = []
    mailer, sheet, ws, gmail, _ = build(approved_rows(1))
    gmail.before_send = lambda raw: seen.append(cell(ws, 2, Col.SEND))
    mailer.run(live=True)
    assert seen == ["Sending"]


def test_pause_between_sends_is_random_between_180_and_420_and_never_before_the_first():
    mailer, sheet, ws, gmail, sleeps = build(approved_rows(8))
    mailer.run(live=True)
    assert len(gmail.sent) == 8 and len(sleeps) == 7
    assert all(180 <= s <= 420 for s in sleeps) and len(set(sleeps)) > 1


def test_the_pause_range_is_exactly_180_to_420():
    class Probe:
        args = None

        def randint(self, low, high):
            Probe.args = (low, high)
            return 200

    mailer, *_ = build(approved_rows(2), rng=Probe())
    mailer.run(live=True)
    assert Probe.args == (180, 420)


def test_the_first_send_happens_before_any_sleep():
    order = []
    mailer, sheet, ws, gmail, sleeps = build(approved_rows(2))
    gmail.before_send = lambda raw: order.append("send")
    mailer._sleep = lambda s: order.append("sleep")
    mailer.run(live=True)
    assert order == ["send", "sleep", "send"]


def test_at_most_15_per_run_and_the_rest_are_reported():
    mailer, sheet, ws, gmail, sleeps = build(approved_rows(20))
    report = mailer.run(live=True)
    assert len(gmail.sent) == 15 and report.remaining == 5 and len(sleeps) == 14
    unsent = [r for r in sheet.read_rows() if r.send_status == "Unsent"]
    assert len(unsent) == 5


def test_draft_mode_is_capped_too():
    mailer, *_ , gmail, _ = build(approved_rows(20))
    report = mailer.run()
    assert len(gmail.drafts) == 15 and report.remaining == 5


def test_cap_can_be_lowered_but_never_raised():
    mailer, *_ = build(approved_rows(1), env={"MAX_EMAILS_PER_RUN": "5"})
    assert mailer.cap() == 5 and mailer.cap(3) == 3 and mailer.cap(99) == 5 and mailer.cap(0) == 5
    mailer, *_ = build(approved_rows(1))
    assert mailer.cap(99) == HARD_CAP_PER_RUN
    forced = dataclasses.replace(mailer.settings, max_per_run=500)
    mailer.settings = forced
    assert mailer.cap() == HARD_CAP_PER_RUN and mailer.cap(500) == HARD_CAP_PER_RUN


def test_limit_argument_lowers_a_single_run():
    mailer, sheet, ws, gmail, _ = build(approved_rows(6))
    report = mailer.run(live=True, limit=2)
    assert len(gmail.sent) == 2 and report.remaining == 4


# --------------------------------------------------------------------------- account lock
def test_wrong_google_account_stops_everything():
    mailer, sheet, ws, gmail, _ = build(approved_rows(2), gmail=FakeGmail(email="varun.personal@gmail.com"))
    with pytest.raises(MailError, match="varun.personal@gmail.com.*pennstatemcvb@gmail.com"):
        mailer.run(live=True)
    assert gmail.calls == ["profile"] and not [c for c in ws.calls if c[0] == "batch_update"]


def test_account_check_is_case_insensitive():
    mailer, *_ = build(approved_rows(1), gmail=FakeGmail(email="PennStateMCVB@Gmail.com"))
    assert mailer.check_account() == "pennstatemcvb@gmail.com"


# --------------------------------------------------------------------------- humans changing rows mid-run
def test_a_row_rejected_while_waiting_between_sends_is_not_sent():
    mailer, sheet, ws, gmail, _ = build(approved_rows(2))
    mailer._sleep = lambda s: ws._set(3, COL[Col.APPROVAL], "Rejected")  # you reject row 2 during the pause
    report = mailer.run(live=True)
    assert [o.status for o in report.outcomes] == ["sent", "skipped"] and len(gmail.sent) == 1
    assert statuses(sheet)["L2"] == ("Rejected", "Unsent") and "Rejected" in report.skipped[0].detail


def test_rejection_in_the_instant_before_the_claim_is_caught_by_the_guard():
    mailer, sheet, ws, gmail, _ = build(approved_rows(1))
    original = sheet.get_row

    def get_row_then_reject(row_id):
        row = original(row_id)
        ws._set(row.sheet_row, COL[Col.APPROVAL], "Rejected")
        return row

    sheet.get_row = get_row_then_reject
    report = mailer.run(live=True)
    assert [o.status for o in report.outcomes] == ["skipped"] and gmail.sent == []
    assert "changed just before sending" in report.skipped[0].detail
    assert statuses(sheet)["L1"] == ("Rejected", "Unsent")


def test_an_edit_between_the_check_and_the_claim_is_never_sent():
    mailer, sheet, ws, gmail, _ = build(approved_rows(1))
    original = sheet.get_row

    def get_row_then_edit(row_id):
        row = original(row_id)
        ws._set(row.sheet_row, COL[Col.BODY], "half-finished edit [First Name]")
        return row

    sheet.get_row = get_row_then_edit
    report = mailer.run(live=True)
    assert gmail.sent == [] and [o.status for o in report.outcomes] == ["skipped"]
    assert cell(ws, 2, Col.SEND) == "Unsent"  # released, so it is checked again next run


def test_a_deleted_row_is_skipped():
    mailer, sheet, ws, gmail, _ = build(approved_rows(2))
    mailer._sleep = lambda s: ws.grid.__setitem__(2, [""] * len(COLUMNS))
    report = mailer.run(live=True)
    assert [o.status for o in report.outcomes] == ["sent", "skipped"] and "deleted" in report.skipped[0].detail


# --------------------------------------------------------------------------- pre-send checks
@pytest.mark.parametrize("extra,needle", [
    ({Col.SUBJECT: ""}, "subject is empty"),
    ({Col.BODY: "Hi [First Name], short"}, "under 20 words"),
    ({Col.BODY: "Hi Jane,\n\n" + GOOD_BODY + "\n\n[Company] rocks"}, "placeholder"),
    ({Col.BODY: "Hi Jane,\n\n" + GOOD_BODY + " Celsius is great."}, "agreed not to mention"),
    ({Col.TARGET_EMAIL: "not-an-email"}, "valid email"),
])
def test_unsafe_rows_are_blocked_with_a_note_and_do_not_use_up_the_cap(extra, needle):
    rows = approved_rows(15) + [approved("LBAD", "BadCo", "bad@bad.com", **extra)]
    mailer, sheet, ws, gmail, _ = build(rows)
    report = mailer.run(live=True)
    assert len(gmail.sent) == 15 and report.remaining == 0
    assert needle in report.blocked[0].detail and report.blocked[0].row_id == "LBAD"
    bad = sheet.get_row("LBAD")
    assert bad.send_status == "Unsent" and "[send] not sent:" in bad.notes and needle in bad.notes


def test_fixing_a_blocked_row_sends_it_and_clears_the_note_but_keeps_human_notes():
    row = approved("L1", **{Col.SUBJECT: "", Col.NOTES: "met at a conference\n[send] not sent: the subject is empty."})
    mailer, sheet, ws, gmail, _ = build([row])
    assert mailer.run(live=True).sent == []
    ws._set(2, COL[Col.SUBJECT], "Now it has a subject")
    report = mailer.run(live=True)
    assert len(report.sent) == 1 and sheet.get_row("L1").notes == "met at a conference"


def test_the_same_address_is_never_mailed_twice():
    rows = [approved("L1", "A", "same@brand.com"), approved("L2", "A again", "SAME@brand.com"),
            approved("L3", "B", "old@brand.com"), approved("L4", "B old", "old@brand.com", **{Col.SEND: "Sent"}),
            approved("L5", "C", "dr@brand.com"), approved("L6", "C drafted", "dr@brand.com", **{Col.SEND: "Drafted"})]
    mailer, sheet, ws, gmail, _ = build(rows)
    report = mailer.run(live=True)
    assert [decode(r)["To"] for r in gmail.sent] == ["same@brand.com"]
    assert sorted(o.row_id for o in report.blocked) == ["L2", "L3", "L5"]
    assert "already emailed" in report.blocked[0].detail


# --------------------------------------------------------------------------- failures: what happens to the row
def test_gmail_rejecting_one_message_fails_only_that_row():
    mailer, sheet, ws, gmail, _ = build(approved_rows(3))
    gmail.send_script = [None, call("row", "Gmail rejected the address", 400), None]
    report = mailer.run(live=True)
    assert [o.status for o in report.outcomes] == ["sent", "failed", "sent"]
    assert statuses(sheet)["L2"] == ("Approved", "Failed") and "Gmail rejected" in sheet.get_row("L2").notes


def test_an_account_or_quota_problem_puts_the_row_back_and_stops_the_run():
    mailer, sheet, ws, gmail, _ = build(approved_rows(3))
    gmail.send_script = [call("run", "Gmail is rate limiting this account", 429)]
    report = mailer.run(live=True)
    assert [o.status for o in report.outcomes] == ["reverted"] and report.aborted
    assert [s[1] for s in statuses(sheet).values()] == ["Unsent"] * 3 and "rate limiting" in sheet.get_row("L1").notes
    assert len(gmail.sent) == 0 and gmail.calls.count("send") == 1


def test_an_unknown_outcome_leaves_the_row_in_sending_stops_and_is_never_retried():
    mailer, sheet, ws, gmail, _ = build(approved_rows(3))
    gmail.send_script = [call("unknown", "The connection to Gmail broke (ReadTimeout)")]
    report = mailer.run(live=True)
    assert [o.status for o in report.outcomes] == ["unknown"] and report.aborted
    row = sheet.get_row("L1")
    assert row.send_status == "Sending" and "UNKNOWN" in row.notes and "Sent folder" in row.notes
    assert "Sent folder" in report.unknown[0].detail
    assert statuses(sheet)["L2"] == ("Approved", "Unsent")

    again = mailer.run(live=True)  # the next run must NOT touch the Sending row
    assert [o.row_id for o in again.sent] == ["L2", "L3"] and statuses(sheet)["L1"][1] == "Sending"
    assert [decode(r)["To"] for r in gmail.sent] == ["p2@brand2.com", "p3@brand3.com"]


def test_three_failures_in_a_row_stop_the_run_but_a_success_resets_the_count():
    mailer, sheet, ws, gmail, _ = build(approved_rows(6))
    gmail.send_script = [call("row", "x", 400)] * 3
    report = mailer.run(live=True)
    assert len(report.failed) == 3 and "in a row" in report.aborted and gmail.calls.count("send") == 3

    mailer, sheet, ws, gmail, _ = build(approved_rows(6))
    gmail.send_script = [call("row", "x", 400), call("row", "x", 400), None, call("row", "x", 400), call("row", "x", 400), None]
    report = mailer.run(live=True)
    assert not report.aborted and len(report.failed) == 4 and len(report.sent) == 2


def test_if_the_sheet_cannot_be_updated_after_a_send_the_row_stays_sending_and_the_run_stops():
    mailer, sheet, ws, gmail, _ = build(approved_rows(2))
    real = sheet.update_row

    def flaky(row_id, updates, expect=None):
        if Col.SENT_AT in updates:
            raise SheetError("Google Sheets is down")
        return real(row_id, updates, expect=expect)

    sheet.update_row = flaky
    report = mailer.run(live=True)
    assert [o.status for o in report.outcomes] == ["sent"] and "SENT to p1@brand1.com" in report.sent[0].detail
    assert report.aborted and len(gmail.sent) == 1 and cell(ws, 2, Col.SEND) == "Sending"


def test_ctrl_c_while_waiting_stops_cleanly():
    def interrupt(seconds):
        raise KeyboardInterrupt

    mailer, sheet, ws, gmail, _ = build(approved_rows(3))
    mailer._sleep = interrupt
    report = mailer.run(live=True)
    assert report.interrupted and len(gmail.sent) == 1
    assert [s[1] for s in statuses(sheet).values()] == ["Sent", "Unsent", "Unsent"]


def test_ctrl_c_during_the_send_itself_is_flagged_for_a_human_to_check():
    mailer, sheet, ws, gmail, _ = build(approved_rows(2))

    def interrupt(raw):
        raise KeyboardInterrupt

    gmail.before_send = interrupt
    report = mailer.run(live=True)
    assert report.interrupted and report.unknown and "Sent folder" in report.unknown[0].detail
    row = sheet.get_row("L1")
    assert row.send_status == "Sending" and "Interrupted" in row.notes


# --------------------------------------------------------------------------- failures in draft mode
def test_draft_rejected_by_gmail_marks_the_row_failed():
    mailer, sheet, ws, gmail, _ = build(approved_rows(2))
    gmail.draft_script = [call("row", "bad message", 400)]
    report = mailer.run()
    assert [o.status for o in report.outcomes] == ["failed", "drafted"]


def test_draft_account_problem_leaves_the_row_unsent_and_stops():
    mailer, sheet, ws, gmail, _ = build(approved_rows(2))
    gmail.draft_script = [call("run", "Gmail refused access", 403)]
    report = mailer.run()
    assert [o.status for o in report.outcomes] == ["reverted"] and report.aborted
    assert statuses(sheet)["L1"] == ("Approved", "Unsent") and "Gmail refused access" in sheet.get_row("L1").notes


def test_draft_with_unknown_outcome_warns_about_a_possible_duplicate():
    mailer, sheet, ws, gmail, _ = build(approved_rows(2))
    gmail.draft_script = [call("unknown", "timeout")]
    report = mailer.run()
    assert [o.status for o in report.outcomes] == ["unknown"] and "Drafts" in report.unknown[0].detail and report.aborted
    assert "may or may not exist" in sheet.get_row("L1").notes


# --------------------------------------------------------------------------- plan, usage, sync
def test_plan_previews_without_writing_anything():
    rows = approved_rows(17) + [approved("LBAD", "Bad", "bad@bad.com", **{Col.SUBJECT: ""})]
    mailer, sheet, ws, gmail, _ = build(rows)
    ready, blocked, remaining = mailer.plan()
    assert len(ready) == 15 and remaining == 2 and [b[0].row_id for b in blocked] == ["LBAD"]
    assert "subject is empty" in blocked[0][1][0]
    assert not [c for c in ws.calls if c[0] in ("batch_update", "update", "append_rows")] and gmail.calls == []


def test_plan_flags_duplicate_addresses_within_the_batch():
    mailer, *_ = build([approved("L1", "A", "dup@x.com"), approved("L2", "B", "dup@x.com")])
    ready, blocked, _ = mailer.plan()
    assert [r.row_id for r in ready] == ["L1"] and blocked[0][0].row_id == "L2"


def test_sent_in_the_last_24_hours():
    old = (NOW - timedelta(hours=30)).isoformat()
    recent = (NOW - timedelta(hours=2)).isoformat()
    rows = [approved("L1", **{Col.SEND: "Sent", Col.SENT_AT: recent}), approved("L2", email="b@x.com", **{Col.SEND: "Sent", Col.SENT_AT: old}),
            approved("L3", email="c@x.com", **{Col.SEND: "Sent", Col.SENT_AT: "garbage"}), approved("L4", email="d@x.com")]
    mailer, *_ = build(rows)
    assert mailer.sent_in_last_24h() == 1


def test_sync_marks_drafts_that_left_gmail_as_sent_and_keeps_the_rest():
    rows = [approved("L1", **{Col.SEND: "Drafted", Col.GMAIL_ID: "d1", Col.NOTES: "human note"}),
            approved("L2", email="b@x.com", **{Col.SEND: "Drafted", Col.GMAIL_ID: "gone"}),
            approved("L3", email="c@x.com", **{Col.SEND: "Drafted", Col.GMAIL_ID: ""}),
            approved("L4", email="d@x.com")]
    mailer, sheet, ws, gmail, _ = build(rows)
    gmail.drafts["d1"] = "raw"
    report = mailer.sync_drafts()
    assert (report.checked, report.still_drafts, len(report.now_sent)) == (2, 1, 1)
    assert statuses(sheet)["L2"] == ("Approved", "Sent") and "left Gmail Drafts" in sheet.get_row("L2").notes
    assert sheet.get_row("L1").send_status == "Drafted" and sheet.get_row("L1").notes == "human note"
    assert sheet.get_row("L3").send_status == "Drafted" and sheet.get_row("L4").send_status == "Unsent"


def test_sync_stops_on_a_gmail_error_and_checks_the_account():
    mailer, sheet, ws, gmail, _ = build([approved("L1", **{Col.SEND: "Drafted", Col.GMAIL_ID: "d1"})])
    gmail.exists_script = [call("run", "Gmail refused access", 403)]
    report = mailer.sync_drafts()
    assert "refused access" in report.aborted and sheet.get_row("L1").send_status == "Drafted"
    wrong, *_ = build([], gmail=FakeGmail(email="x@gmail.com"))
    with pytest.raises(MailError):
        wrong.sync_drafts()


# --------------------------------------------------------------------------- building the message
def test_message_headers_and_body():
    raw = M.build_raw_message(Sender(), "jane@cerave.com", " Penn State Men's Club Volleyball ", "Hi Jane,\r\n\r\nLine one.\r\n\r\nBest,\r\nVarun")
    msg = decode(raw)
    assert msg["From"] == "Varun Kaveti <pennstatemcvb@gmail.com>" and msg["To"] == "jane@cerave.com"
    assert msg["Subject"] == "Penn State Men's Club Volleyball" and msg["Bcc"] is None and msg["Cc"] is None
    assert msg.get_content().replace("\r\n", "\n") == "Hi Jane,\n\nLine one.\n\nBest,\nVarun\n"
    assert "=" not in raw and "+" not in raw and "/" not in raw  # base64url alphabet


def test_non_ascii_text_survives():
    e = chr(0xE9)
    msg = decode(M.build_raw_message(Sender(), "a@b.com", f"Caf{e} partnership", f"Hi Jos{e},\n\nThanks."))
    assert msg["Subject"] == f"Caf{e} partnership" and f"Jos{e}" in msg.get_content()


@pytest.mark.parametrize("subject,to", [("bad\nBcc: evil@x.com", "a@b.com"), ("ok", "a@b.com\nBcc: evil@x.com"), ("bad\rsubject", "a@b.com")])
def test_header_injection_is_impossible(subject, to):
    with pytest.raises(ValueError):
        M.build_raw_message(Sender(), to, subject, "body")


# --------------------------------------------------------------------------- GmailApi: classification with a fake session
class FakeSession:
    def __init__(self, *items):
        self.items, self.requests, self.headers = list(items), [], []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.requests.append((method, url, json, timeout))
        self.headers.append(headers)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def http(status, payload=None, raw=None):
    response = requests.Response()
    response.status_code = status
    response._content = raw if raw is not None else json.dumps(payload if payload is not None else {}).encode()
    return response


def gmail_error(status, reason="", message="nope"):
    return http(status, {"error": {"code": status, "message": message, "errors": [{"reason": reason}] if reason else []}})


def api(*items):
    return M.GmailApi(FakeSession(*items), base_url="https://gmail.test/v1/users/me"), None


def test_successful_calls_hit_the_right_endpoints():
    session = FakeSession(http(200, {"emailAddress": "PennStateMCVB@gmail.com"}), http(200, {"id": "d9"}), http(200, {"id": "m9"}), http(200, {}))
    gmail = M.GmailApi(session, base_url="https://gmail.test/v1/users/me/")
    assert gmail.profile_email() == "pennstatemcvb@gmail.com"
    assert gmail.create_draft("RAW") == "d9" and gmail.send_raw("RAW") == "m9" and gmail.draft_exists("d9") is True
    assert [(m, u) for m, u, _, _ in session.requests] == [
        ("GET", "https://gmail.test/v1/users/me/profile"), ("POST", "https://gmail.test/v1/users/me/drafts"),
        ("POST", "https://gmail.test/v1/users/me/messages/send"), ("GET", "https://gmail.test/v1/users/me/drafts/d9")]
    assert session.requests[1][2] == {"message": {"raw": "RAW"}} and session.requests[2][2] == {"raw": "RAW"}
    assert all(t == M.REQUEST_TIMEOUT for _, _, _, t in session.requests)
    assert all(h == {"Connection": "close"} for h in session.headers)  # a fresh socket for every call


@pytest.mark.parametrize("status,reason,kind", [
    (400, "invalidArgument", "row"), (413, "", "row"), (400, "failedPrecondition", "run"),
    (401, "", "run"), (403, "", "run"), (403, "rateLimitExceeded", "run"), (404, "", "run"), (429, "", "run"),
    (408, "", "unknown"), (409, "", "unknown"), (500, "", "unknown"), (502, "", "unknown"), (503, "", "unknown"),
])
def test_http_errors_are_classified_by_what_they_mean_for_the_row(status, reason, kind):
    gmail = M.GmailApi(FakeSession(gmail_error(status, reason)))
    with pytest.raises(M.GmailCallError) as info:
        gmail.send_raw("RAW")
    assert info.value.kind == kind and info.value.status == status


def test_failures_before_the_request_leaves_are_safe_and_failures_after_are_unknown():
    from urllib3.exceptions import MaxRetryError, NewConnectionError, NameResolutionError, ProtocolError, SSLError

    def conn_error(reason):
        return requests.exceptions.ConnectionError(MaxRetryError(None, "/x", reason=reason))

    safe = [requests.exceptions.ConnectTimeout(), conn_error(NewConnectionError(None, "refused")), conn_error(NameResolutionError("h", None, "dns"))]
    risky = [requests.exceptions.ReadTimeout(), requests.exceptions.ConnectionError(ProtocolError("reset")),
             conn_error(SSLError("eof")), requests.exceptions.ChunkedEncodingError("cut")]
    for exc in safe:
        with pytest.raises(M.GmailCallError) as info:
            M.GmailApi(FakeSession(exc)).send_raw("RAW")
        assert info.value.kind == "run", repr(exc)
    for exc in risky:
        with pytest.raises(M.GmailCallError) as info:
            M.GmailApi(FakeSession(exc)).send_raw("RAW")
        assert info.value.kind == "unknown", repr(exc)


def test_login_failures_are_safe_and_tell_you_to_sign_in_again():
    with pytest.raises(M.GmailCallError, match="python main.py auth") as info:
        M.GmailApi(FakeSession(RefreshError("invalid_grant"))).send_raw("RAW")
    assert info.value.kind == "run"


def test_unreadable_or_incomplete_success_replies_are_unknown():
    for item in (http(200, raw=b"<html>"), http(200, {})):
        with pytest.raises(M.GmailCallError) as info:
            M.GmailApi(FakeSession(item)).send_raw("RAW")
        assert info.value.kind == "unknown"
    with pytest.raises(M.GmailCallError) as info:
        M.GmailApi(FakeSession(http(200, {}))).create_draft("RAW")
    assert info.value.kind == "unknown"


def test_draft_exists_distinguishes_missing_from_broken():
    assert M.GmailApi(FakeSession(gmail_error(404))).draft_exists("d1") is False
    with pytest.raises(M.GmailCallError):
        M.GmailApi(FakeSession(gmail_error(403))).draft_exists("d1")


def test_gmail_error_text_is_actionable():
    with pytest.raises(M.GmailCallError, match="not enabled"):
        M.GmailApi(FakeSession(gmail_error(403, "accessNotConfigured", "Gmail API has not been used in project 1"))).send_raw("RAW")


# --------------------------------------------------------------------------- GmailApi over real sockets
class LocalServer(HTTPServer):
    """HTTPServer without the reverse-DNS lookup of its own address, which can stall for ~35s on a Mac."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@pytest.fixture
def server():
    class State:
        status, payload, seen = 200, b'{"id": "m1"}', []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            State.seen.append((self.path, self.headers.get("Content-Type"), json.loads(body)))
            self.send_response(State.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(State.payload)))
            self.end_headers()
            self.wfile.write(State.payload)

        def log_message(self, *args):
            pass

    State.seen = []
    httpd = LocalServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    State.url = f"http://127.0.0.1:{httpd.server_port}/gmail/v1/users/me"
    yield State
    httpd.shutdown()


def test_a_real_http_round_trip_sends_json_and_parses_the_id(server):
    gmail = M.GmailApi(requests.Session(), base_url=server.url)
    assert gmail.send_raw("QUJD") == "m1"
    assert server.seen == [("/gmail/v1/users/me/messages/send", "application/json", {"raw": "QUJD"})]


def test_a_real_500_is_an_unknown_outcome(server):
    server.status, server.payload = 500, b'{"error": {"code": 500, "message": "backend error"}}'
    with pytest.raises(M.GmailCallError) as info:
        M.GmailApi(requests.Session(), base_url=server.url).send_raw("QUJD")
    assert info.value.kind == "unknown" and len(server.seen) == 1  # exactly one attempt: no hidden retry


def test_a_refused_connection_is_safe_to_retry_later():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(M.GmailCallError) as info:
        M.GmailApi(requests.Session(), timeout=(2, 2), base_url=f"http://127.0.0.1:{port}/x").send_raw("QUJD")
    assert info.value.kind == "run"


def test_a_server_that_reads_the_request_and_never_answers_is_unknown():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    received = []

    def silent():
        conn, _ = srv.accept()
        conn.settimeout(1.0)
        data = b""
        try:
            while b"QUJD" not in data:  # headers and body can arrive in separate packets
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        received.append(data)
        threading.Event().wait(1.5)
        conn.close()

    threading.Thread(target=silent, daemon=True).start()
    gmail = M.GmailApi(requests.Session(), timeout=(2, 0.4), base_url=f"http://127.0.0.1:{srv.getsockname()[1]}/x")
    with pytest.raises(M.GmailCallError) as info:
        gmail.send_raw("QUJD")
    srv.close()
    assert info.value.kind == "unknown" and received and b"QUJD" in received[0]


def test_plan_matches_run_for_a_hand_typed_row_with_a_blank_send_status():
    row = approved("L1", **{Col.SEND: ""})
    mailer, sheet, ws, gmail, _ = build([row])
    ready, blocked, remaining = mailer.plan()
    assert [r.row_id for r in ready] == ["L1"] and blocked == [] and remaining == 0
    assert len(mailer.run(live=True).sent) == 1


def test_note_blocked_writes_reasons_to_the_sheet_and_touches_nothing_else():
    rows = [approved("L1"), approved("L2", "Bad", "bad@bad.com", **{Col.SUBJECT: ""})]
    mailer, sheet, ws, gmail, _ = build(rows)
    ready, blocked, _ = mailer.plan()
    mailer.note_blocked(blocked)
    assert "[send] not sent: the subject is empty" in sheet.get_row("L2").notes and sheet.get_row("L1").notes == ""
    assert statuses(sheet) == {"L1": ("Approved", "Unsent"), "L2": ("Approved", "Unsent")}
    assert gmail.calls == [] and gmail.sent == [] and gmail.drafts == {}  # a note is not a send, or even a Gmail call


def test_a_note_is_not_written_over_one_a_human_just_typed():
    mailer, sheet, ws, gmail, _ = build([approved("L1", **{Col.SUBJECT: "", Col.NOTES: "old note"})])
    _, blocked, _ = mailer.plan()
    ws.before_read = lambda w: w._set(2, COL[Col.NOTES], "the human wrote this a moment ago")
    mailer.note_blocked(blocked)
    assert sheet.get_row("L1").notes == "the human wrote this a moment ago"
