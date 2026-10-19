import json
import stat
from datetime import timedelta

import pytest

import src.cli as C
from config.errors import DiscoveryAuthError, DiscoveryError, LLMAuthError, QuotaExceeded
from config.settings import COLUMNS, Col
from src import discovery as D
from src.cli import classify, main
from src.models import LeadRow
from tests.fakes import FakeGmail, FakeGspreadClient, FakeSpreadsheet, FakeWorksheet
from tests.harness import COL, NOW, Harness
from tests.samples import GOOD_BODY, FakeLLM, approved, pending



@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def cell(harness, row, column):
    return harness.ws.cell(row, COL[column])


# --------------------------------------------------------------------------- entry point behaviour
def test_no_command_prints_help_and_succeeds(capsys):
    assert main([]) == 0 and "configure" in capsys.readouterr().out


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for name in C.COMMANDS:
        assert name in out


def test_unknown_command_and_bad_numbers_are_argparse_errors(capsys):
    with pytest.raises(SystemExit) as info:
        main(["frobnicate"])
    assert info.value.code == 2
    with pytest.raises(SystemExit):
        main(["draft", "--limit", "many"])


def test_known_errors_print_one_line_and_exit_1(h, capsys, monkeypatch):
    monkeypatch.setitem(C.COMMANDS, "status", lambda rt, args: (_ for _ in ()).throw(LLMAuthError("the key is wrong")))
    assert h.run("status") == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "Error: the key is wrong" and "Traceback" not in captured.err


def test_ctrl_c_exits_130_and_unexpected_errors_exit_2_without_a_traceback(h, capsys, monkeypatch):
    monkeypatch.setitem(C.COMMANDS, "status", lambda rt, args: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert h.run("status") == 130 and "Stopped by you" in capsys.readouterr().err
    monkeypatch.setitem(C.COMMANDS, "status", lambda rt, args: 1 / 0)
    assert h.run("status") == 2
    err = capsys.readouterr().err
    assert "Unexpected error (ZeroDivisionError)" in err and "outreach.log" in err and "Traceback" not in err


# --------------------------------------------------------------------------- configure
def test_configure_saves_a_piped_key_privately_and_checks_it(tmp_path, monkeypatch):
    fake_key = "sk-ant-api03-fakekey1"
    h = Harness(tmp_path, read_stdin=lambda: f"  {fake_key}\n")
    checked = []
    monkeypatch.setattr(C, "build_llm", lambda settings: type("L", (), {"check": lambda self: checked.append(settings.anthropic_api_key) or "Anthropic key accepted"})())
    assert h.run("configure", "--provider", "anthropic", "--key-from-stdin") == 0
    text = h.env_path.read_text()
    assert f"ANTHROPIC_API_KEY={fake_key}" in text and "LLM_PROVIDER=anthropic" in text
    assert stat.S_IMODE(h.env_path.stat().st_mode) == 0o600
    assert checked == [fake_key] and "fakekey1" not in h.text  # never echoed in full
    assert "Anthropic key accepted" in h.text


def test_configure_reports_a_failed_key_check_but_keeps_the_key(tmp_path, monkeypatch):
    h = Harness(tmp_path, read_stdin=lambda: "sk-ant-bad-key-1234567890")
    monkeypatch.setattr(C, "build_llm", lambda settings: type("L", (), {"check": lambda self: (_ for _ in ()).throw(LLMAuthError("Anthropic rejected the key"))})())
    assert h.run("configure", "--provider", "anthropic", "--key-from-stdin") == 1
    assert "sk-ant-bad-key-1234567890" in h.env_path.read_text() and "check failed" in h.text


def test_configure_warns_about_an_odd_looking_key_and_supports_gemini(tmp_path):
    h = Harness(tmp_path, read_stdin=lambda: "not-a-google-key")
    assert h.run("configure", "--provider", "gemini", "--key-from-stdin", "--no-check") == 0
    assert "normally start with AIza" in h.text
    assert "GEMINI_API_KEY=not-a-google-key" in h.env_path.read_text() and "LLM_PROVIDER=gemini" in h.env_path.read_text()


def test_configure_with_nothing_piped_is_an_error(tmp_path, capsys):
    h = Harness(tmp_path, read_stdin=lambda: "  \n")
    assert h.run("configure", "--key-from-stdin") == 1 and "Nothing was piped in" in capsys.readouterr().err


def test_configure_interactive_asks_in_order_and_keeps_blank_answers(tmp_path):
    gemini_fake = "AIza-secret-key-123456"
    asked, secrets = [], iter([gemini_fake, "hunter-key-abcdef", ""])
    answers = iter(["gemini", "me@psu.edu"])
    h = Harness(tmp_path, interactive=True, ask=lambda q: asked.append(q) or next(answers), ask_secret=lambda q: next(secrets))
    assert h.run("configure", "--no-check") == 0
    text = h.env_path.read_text()
    assert "LLM_PROVIDER=gemini" in text and f"GEMINI_API_KEY={gemini_fake}" in text
    assert "HUNTER_API_KEY=hunter-key-abcdef" in text and "SHARE_SHEET_WITH=me@psu.edu" in text
    assert "\nAPOLLO_API_KEY=\n" in text  # skipped, so it stays blank


def test_configure_rejects_an_unknown_provider(tmp_path, capsys):
    h = Harness(tmp_path, interactive=True, ask=lambda q: "openai", ask_secret=lambda q: "")
    assert h.run("configure") == 1 and "'anthropic' or 'gemini'" in capsys.readouterr().err


# --------------------------------------------------------------------------- init-sheet
def test_init_sheet_creates_the_sheet_and_saves_its_id(tmp_path):
    client = FakeGspreadClient()
    h = Harness(tmp_path, env={}, gspread_client=client, sheet=None)
    assert h.run("init-sheet", "--share", "me@psu.edu", "--share", "me@psu.edu") == 0
    assert client.spreadsheet.sheet1.grid[0] == list(COLUMNS) and client.spreadsheet.shared_with == ["me@psu.edu"]
    assert "SPREADSHEET_ID=SHEET123" in h.env_path.read_text() and "docs.google.com/spreadsheets/d/SHEET123" in h.text
    assert stat.S_IMODE(h.env_path.stat().st_mode) == 0o600


def test_init_sheet_refuses_to_create_a_second_sheet_by_accident(h, capsys):
    assert h.run("init-sheet") == 1
    err = capsys.readouterr().err
    assert "already set up" in err and "--repair" in err and "SHEET123" in err


def test_init_sheet_new_flag_creates_another(tmp_path):
    client = FakeGspreadClient()
    h = Harness(tmp_path, gspread_client=client, sheet=None)
    assert h.run("init-sheet", "--new") == 0 and client.created_titles


def test_init_sheet_repair_adds_missing_columns_without_touching_rows(tmp_path):
    short = list(COLUMNS)[:-2]
    h = Harness(tmp_path)
    h.ws.grid = [short, ["L1", "Keep Me"]]
    assert h.run("init-sheet", "--repair") == 0
    assert "Added missing columns: Notes, Company Domain" in h.text and h.ws.grid[1][:2] == ["L1", "Keep Me"]


# --------------------------------------------------------------------------- import
CSV_WITH_EMAILS = (
    "Company Name,Target Email,Target Name,Category,Custom Hook / Angle\n"
    "KT Tape,pat@kttape.com,Pat Lee,Recovery,taped wrists\n"
    "Liquid I.V.,sam@liquid-iv.com,,Hydration,\n"
    "Celsius,x@celsius.com,,Energy,\n"
)


def test_import_adds_contacts_skips_banned_and_reports_duplicates(h, tmp_path):
    path = tmp_path / "contacts.csv"
    path.write_text(CSV_WITH_EMAILS)
    assert h.run("import", "--csv", str(path)) == 0
    rows = h.sheet.read_rows()
    assert [(r.company, r.email, r.approval, r.send_status) for r in rows] == [
        ("KT Tape", "pat@kttape.com", "Pending", "Unsent"), ("Liquid I.V.", "sam@liquid-iv.com", "Pending", "Unsent")]
    assert "Added 2 rows" in h.text and "BANNED_BRANDS" in h.text and "Next: python main.py draft" in h.text
    h.lines.clear()
    assert h.run("import", "--csv", str(path)) == 0
    assert "Added 0 rows to the sheet, skipped 2 duplicates" in h.text and len(h.sheet.read_rows()) == 2


def test_import_of_brands_without_emails_explains_the_two_options(h, tmp_path):
    path = tmp_path / "brands.csv"
    path.write_text("Company Name,Domain,Category\nRed Bull,redbull.com,Energy\n")
    assert h.run("import", "--csv", str(path)) == 0
    assert h.sheet.read_rows() == [] and "no email address" in h.text and "discover" in h.text


def test_import_without_a_file_tells_you_how_to_start(h, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(C, "DEFAULT_TARGETS_CSV", tmp_path / "targets.csv")
    assert h.run("import") == 1
    err = capsys.readouterr().err
    assert "No brand list found" in err and "targets.example.csv" in err and "--csv PATH" in err and "--sheet URL" in err


def test_import_reads_another_google_sheet(h):
    other = FakeWorksheet(title="Targets", grid=[["Company Name", "Target Email", "Category"], ["Therabody", "ana@therabody.com", "Recovery"]])
    h.rt._gspread = FakeGspreadClient(FakeSpreadsheet(other, sheet_id="OTHER1"))
    assert h.run("import", "--sheet", "https://docs.google.com/spreadsheets/d/OTHER1/edit", "--tab", "Targets") == 0
    assert [r.company for r in h.sheet.read_rows()] == ["Therabody"]


# --------------------------------------------------------------------------- discover
class Stub:
    name = "Stub"

    def __init__(self, script):
        self.script, self.seen = script, []

    def find_contacts(self, target, **kw):
        self.seen.append(target.company)
        item = self.script.get(target.company, D.LookupResult())
        if isinstance(item, Exception):
            raise item
        return item


def found(email, domain, position="Partnerships Manager"):
    return D.LookupResult([D.Contact(email, "Jo", "Ng", position, 90, "valid", "Stub")], domain, "", 1)


@pytest.fixture
def brands_csv(tmp_path):
    path = tmp_path / "brands.csv"
    path.write_text("Company Name,Domain,Category\nRed Bull,redbull.com,Energy\nCeraVe,cerave.com,Skincare\nCelsius,celsius.com,Energy\nNuun,nuunlife.com,Hydration\n")
    return path


def test_discover_dry_run_spends_nothing_and_adds_nothing(h, brands_csv):
    h.rt._backend = Stub({})
    assert h.run("discover", "--csv", str(brands_csv), "--dry-run") == 0
    assert "would look up: Red Bull" in h.text and "Dry run" in h.text and "existing partner" in h.text
    assert h.rt._backend.seen == [] and h.sheet.read_rows() == []


def test_discover_states_the_worst_case_hunter_credit_cost_up_front(h, brands_csv):
    h.rt._backend = Stub({})
    assert h.run("discover", "--csv", str(brands_csv), "--dry-run") == 0
    assert "up to 6 credits for this run" in h.text and "50 credits a month" in h.text and "--no-broad" in h.text  # 3 brands (Celsius is skipped)


def test_discover_adds_found_contacts_with_a_provenance_note(h, brands_csv):
    h.rt._backend = Stub({"Red Bull": found("jo@redbull.com", "redbull.com"), "CeraVe": found("al@cerave.com", "cerave.com", "Brand Marketing Manager")})
    assert h.run("discover", "--csv", str(brands_csv)) == 0
    rows = {r.company: r for r in h.sheet.read_rows()}
    assert set(rows) == {"Red Bull", "CeraVe"} and rows["Red Bull"].category == "Energy" and rows["Red Bull"].domain == "redbull.com"
    assert rows["CeraVe"].notes.startswith("[found] Stub: Brand Marketing Manager") and rows["Red Bull"].target_name == "Jo Ng"
    assert "no usable contact found for: Nuun" in h.text and "Added 2 rows" in h.text and "Celsius" not in h.sheet.read_rows()[0].company


def test_discover_skips_brands_already_in_the_sheet(tmp_path, brands_csv):
    h = Harness(tmp_path, rows=[pending("L1", "Red Bull", "old@redbull.com", **{Col.DOMAIN: "redbull.com"})])
    h.rt._backend = Stub({"CeraVe": found("al@cerave.com", "cerave.com")})
    assert h.run("discover", "--csv", str(brands_csv)) == 0
    assert "Red Bull: redbull.com is already in the sheet" in h.text and "Red Bull" not in h.rt._backend.seen


def test_discover_keeps_findings_and_exits_1_when_the_quota_runs_out(h, brands_csv):
    h.rt._backend = Stub({"Red Bull": found("jo@redbull.com", "redbull.com"), "CeraVe": QuotaExceeded("Your Hunter plan has no searches left")})
    assert h.run("discover", "--csv", str(brands_csv)) == 1
    assert [r.company for r in h.sheet.read_rows()] == ["Red Bull"]
    assert "Stopped early: Your Hunter plan has no searches left" in h.text and "were not looked up" in h.text


def test_discover_stops_on_a_rejected_key(h, brands_csv):
    h.rt._backend = Stub({"Red Bull": DiscoveryAuthError("Hunter rejected HUNTER_API_KEY")})
    assert h.run("discover", "--csv", str(brands_csv)) == 1 and h.sheet.read_rows() == []


def test_discover_without_a_key_explains_the_csv_alternative(h, brands_csv, capsys):
    assert h.run("discover", "--csv", str(brands_csv)) == 1
    err = capsys.readouterr().err
    assert "HUNTER_API_KEY is empty" in err and "import --csv" in err


def test_discover_imports_rows_that_already_have_an_email_directly(h, tmp_path):
    path = tmp_path / "mixed.csv"
    path.write_text("Company Name,Domain,Target Email,Category\nKT Tape,kttape.com,pat@kttape.com,Recovery\nRed Bull,redbull.com,,Energy\n")
    h.rt._backend = Stub({"Red Bull": found("jo@redbull.com", "redbull.com")})
    assert h.run("discover", "--csv", str(path)) == 0
    assert sorted(r.company for r in h.sheet.read_rows()) == ["KT Tape", "Red Bull"]


# --------------------------------------------------------------------------- draft
def test_draft_writes_emails_and_points_to_the_next_step(tmp_path):
    h = Harness(tmp_path, rows=[pending("L1"), pending("L2", "Nuun", "sam@nuun.com")])
    assert h.run("draft") == 0
    rows = h.sheet.read_rows()
    assert [r.approval for r in rows] == ["Pending Approval"] * 2 and rows[0].body.startswith("Hi Jane,")
    assert "Drafted 2 emails; 0 failed; 0 skipped" in h.text and "Approved" in h.text and "send" in h.text


def test_draft_dry_run_prints_the_email_and_changes_nothing(tmp_path):
    h = Harness(tmp_path, rows=[pending("L1")])
    before = [list(r) for r in h.ws.grid]
    assert h.run("draft", "--dry-run") == 0
    assert "Previewed 1 email" in h.text and "Hi Jane," in h.text and "Personal Instagram: @varunkaveti" in h.text
    assert h.ws.grid == before


def test_draft_limit_and_row_filters(tmp_path):
    h = Harness(tmp_path, rows=[pending(f"L{i}", f"Brand{i}", f"p{i}@b{i}.com") for i in range(1, 5)])
    assert h.run("draft", "--limit", "2") == 0
    assert "2 more Pending rows not drafted because of the limit of 2" in h.text
    h.lines.clear()
    assert h.run("draft", "--row", "L4") == 0 and h.sheet.get_row("L4").approval == "Pending Approval"


def test_draft_with_a_rejected_key_exits_1_and_says_why(tmp_path):
    h = Harness(tmp_path, rows=[pending("L1")])
    h.rt._llm = FakeLLM(LLMAuthError("Anthropic rejected ANTHROPIC_API_KEY"))
    assert h.run("draft") == 1 and "Stopped early: Anthropic rejected" in h.text and h.sheet.get_row("L1").approval == "Pending"


def test_draft_without_a_key_fails_with_the_fix(tmp_path, capsys):
    h = Harness(tmp_path, rows=[pending("L1")], llm=None)
    assert h.run("draft") == 1 and "ANTHROPIC_API_KEY is empty" in capsys.readouterr().err


# --------------------------------------------------------------------------- send
def test_send_creates_gmail_drafts_by_default_and_never_sends(tmp_path):
    h = Harness(tmp_path, rows=[approved("L1"), approved("L2", "Nuun", "sam@nuun.com")])
    assert h.run("send") == 0
    assert len(h.gmail.drafts) == 2 and h.gmail.sent == [] and "send" not in h.gmail.calls
    assert [r.send_status for r in h.sheet.read_rows()] == ["Drafted", "Drafted"]
    assert "Created 2 Gmail drafts" in h.text and "Open Gmail as pennstatemcvb@gmail.com" in h.text and "sync" in h.text


def test_send_preview_changes_nothing_and_shows_blocked_rows(tmp_path):
    h = Harness(tmp_path, rows=[approved("L1"), approved("L2", "Bad", "bad@x.com", **{Col.SUBJECT: ""})])
    before = [list(r) for r in h.ws.grid]
    assert h.run("send", "--preview", "--live") == 0
    assert "1 email would be processed, 1 blocked" in h.text and "BLOCKED" in h.text and "LIVE" in h.text
    assert h.ws.grid == before and h.gmail.calls == []


def test_send_with_nothing_approved_says_so(tmp_path):
    h = Harness(tmp_path, rows=[pending("L1")])
    assert h.run("send") == 0 and "Nothing to send" in h.text and "Approve rows" in h.text and h.gmail.drafts == {}


def test_live_send_needs_a_typed_confirmation(tmp_path):
    answers = []
    h = Harness(tmp_path, rows=[approved("L1")], interactive=True, ask=lambda q: answers.append(q) or "send it")
    assert h.run("send", "--live") == 0
    assert h.gmail.sent == [] and "Cancelled" in h.text and "Type SEND" in answers[0]
    h2 = Harness(tmp_path, rows=[approved("L1")], interactive=True, ask=lambda q: "SEND")
    assert h2.run("send", "--live") == 0 and len(h2.gmail.sent) == 1 and "Sent 1 email" in h2.text
    assert h2.sheet.get_row("L1").get(Col.SENT_AT) == "2026-10-18T15:30:00+00:00"


def test_live_send_without_a_terminal_or_yes_is_refused(tmp_path, capsys):
    h = Harness(tmp_path, rows=[approved("L1")], interactive=False)
    assert h.run("send", "--live") == 1 and "--yes" in capsys.readouterr().err and h.gmail.sent == []


def test_live_send_with_yes_runs_with_pauses_and_a_time_estimate(tmp_path):
    h = Harness(tmp_path, rows=[approved(f"L{i}", f"B{i}", f"p{i}@b{i}.com") for i in range(1, 4)])
    assert h.run("send", "--live", "--yes") == 0
    assert len(h.gmail.sent) == 3 and len(h.sleeps) == 2 and all(180 <= s <= 420 for s in h.sleeps)
    assert "about 10 minutes" in h.text and "caffeinate" in h.text


def test_send_limit_lowers_the_batch(tmp_path):
    h = Harness(tmp_path, rows=[approved(f"L{i}", f"B{i}", f"p{i}@b{i}.com") for i in range(1, 6)])
    assert h.run("send", "--limit", "2") == 0 and len(h.gmail.drafts) == 2 and "3 approved rows left for the next run" in h.text


def test_send_exits_1_and_warns_loudly_when_an_outcome_is_unknown(tmp_path):
    from src.mailer import GmailCallError
    h = Harness(tmp_path, rows=[approved("L1")])
    h.gmail.send_script = [GmailCallError("unknown", "The connection to Gmail broke")]
    assert h.run("send", "--live", "--yes") == 1
    assert "IMPORTANT" in h.text and "Sent folder" in h.text and h.sheet.get_row("L1").send_status == "Sending"


def test_send_refuses_the_wrong_google_account(tmp_path, capsys):
    h = Harness(tmp_path, rows=[approved("L1")], gmail=FakeGmail(email="someone.else@gmail.com"))
    assert h.run("send") == 1 and "someone.else@gmail.com" in capsys.readouterr().err and h.gmail.drafts == {}


def test_live_send_warns_about_a_high_recent_volume(tmp_path):
    recent = (NOW - timedelta(hours=1)).isoformat()
    sent = [approved(f"S{i}", f"S{i}", f"s{i}@s{i}.com", **{Col.SEND: "Sent", Col.SENT_AT: recent}) for i in range(15)]
    h = Harness(tmp_path, rows=sent + [approved("L1")])
    assert h.run("send", "--live", "--yes") == 0 and "15 emails already went out in the last 24 hours" in h.text


# --------------------------------------------------------------------------- sync / status
def test_sync_marks_hand_sent_drafts(tmp_path):
    h = Harness(tmp_path, rows=[approved("L1", **{Col.SEND: "Drafted", Col.GMAIL_ID: "d1"}), approved("L2", "Nuun", "sam@nuun.com", **{Col.SEND: "Drafted", Col.GMAIL_ID: "gone"})])
    h.gmail.drafts["d1"] = "raw"
    assert h.run("sync") == 0
    assert "marked Sent" in h.text and "1 still waiting" in h.text and h.sheet.get_row("L2").send_status == "Sent"


@pytest.mark.parametrize("approval,send,expected", [
    ("", "", "waiting"), ("Pending", "Unsent", "waiting"), ("Pending Approval", "Unsent", "review"), ("Approved", "", "ready"),
    ("Approved", "Unsent", "ready"), ("approved", "drafted", "drafts"), ("Approved", "Sending", "sending"), ("Approved", "Failed", "failed"),
    ("Approved", "Sent", "sent"), ("Rejected", "Unsent", "rejected"), ("Rejected", "Sent", "rejected"),
    ("Aproved", "Unsent", "odd"), ("Approved", "Sentt", "odd"), ("Pending", "Sent", "odd"), ("Pending Approval", "Drafted", "odd"),
])
def test_rows_are_classified_for_the_status_report(approval, send, expected):
    assert classify(LeadRow({Col.APPROVAL: approval, Col.SEND: send})) == expected


def test_status_summarises_the_sheet_and_the_next_steps(tmp_path):
    rows = [pending("L1"), approved("L2", "Review", "r@r.com", **{Col.APPROVAL: "Pending Approval"}), approved("L3", "Ready", "a@a.com"),
            approved("L4", "Stuck", "s@s.com", **{Col.SEND: "Sending"}), approved("L5", "Done", "d@d.com", **{Col.SEND: "Sent", Col.SENT_AT: (NOW - timedelta(hours=2)).isoformat()})]
    h = Harness(tmp_path, rows=rows)
    assert h.run("status") == 0
    for needle in ("5 rows", "waiting for a draft", "waiting for YOUR review", "approved, ready to send", "STUCK in Sending", "1 email sent in the last 24 hours", "What to do next"):
        assert needle in h.text, needle


def test_status_on_an_empty_sheet_suggests_importing(tmp_path):
    h = Harness(tmp_path)
    assert h.run("status") == 0 and "no rows yet" in h.text


# --------------------------------------------------------------------------- doctor
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(C, "TOKEN_FILE", tmp_path / "token.json")
    monkeypatch.setattr(C, "DEFAULT_TARGETS_CSV", tmp_path / "targets.csv")
    monkeypatch.setattr(C, "_git_hooks_path", lambda: "scripts/hooks")
    return tmp_path


def test_doctor_on_a_fresh_checkout_lists_todos_not_failures(isolated):
    h = Harness(isolated, env={})
    assert h.run("doctor") == 0
    for needle in ("[ ok ] Required packages installed", "Sender: Varun Kaveti <pennstatemcvb@gmail.com>", "[todo] Claude API key not set", "[todo] Google OAuth client file",
                   "[todo] Google sign-in", "[todo] No review sheet yet", "[skip] Hunter key not set", "[ ok ] Secrets guard is on", "setup steps left. Nothing is broken"):
        assert needle in h.text, needle
    assert "[FAIL]" not in h.text


def test_doctor_marks_a_missing_secrets_guard_as_a_todo(isolated, monkeypatch):
    monkeypatch.setattr(C, "_git_hooks_path", lambda: "")
    h = Harness(isolated, env={})
    h.run("doctor")
    assert "[todo] Secrets guard is off" in h.text and "repository is public" in h.text


def test_doctor_fully_configured_offline(isolated):
    from config.credentials import save_credentials
    (isolated / "credentials.json").write_text('{"installed": {"client_id": "x", "client_secret": "y"}}')
    from google.oauth2.credentials import Credentials
    from config.settings import GOOGLE_SCOPES
    save_credentials(Credentials(token="a", refresh_token="r", client_id="c", client_secret="s", scopes=GOOGLE_SCOPES), isolated / "token.json")
    (isolated / "targets.csv").write_text("x")
    (isolated / ".env").write_text("x")
    h = Harness(isolated, env={"ANTHROPIC_API_KEY": "sk-ant-api03-abcdefghijkl", "SPREADSHEET_ID": "S1", "HUNTER_API_KEY": "h"})
    assert h.run("doctor") == 0
    assert "Everything checks out" in h.text and "sk-a...jkl" in h.text and "abcdefghijkl" not in h.text.replace("sk-a...jkl", "")


def test_doctor_online_tests_everything_and_reports_failures(isolated):
    h = Harness(isolated, env={"ANTHROPIC_API_KEY": "sk-ant-x", "SPREADSHEET_ID": "SHEET123"}, creds=object())
    assert h.run("doctor", "--online") == 0
    assert "fake ok" in h.text and "signed in as pennstatemcvb@gmail.com" in h.text and "'Fake Sheet' opens, 0 lead rows" in h.text

    wrong = Harness(isolated, env={"ANTHROPIC_API_KEY": "sk-ant-x", "SPREADSHEET_ID": "SHEET123"}, creds=object(), gmail=FakeGmail(email="x@gmail.com"))
    assert wrong.run("doctor", "--online") == 1
    assert "[FAIL] Google login and Gmail" in wrong.text and "x@gmail.com" in wrong.text and "[skip] Review sheet" in wrong.text

    broken = Harness(isolated, env={"ANTHROPIC_API_KEY": "sk-ant-x"}, creds=object(), llm=FakeLLM("x"))
    broken.rt._llm.check = lambda: (_ for _ in ()).throw(LLMAuthError("Anthropic rejected ANTHROPIC_API_KEY"))
    assert broken.run("doctor", "--online") == 1 and "[FAIL] Claude key and model" in broken.text


def test_doctor_online_survives_surprising_library_errors(isolated):
    h = Harness(isolated, env={"ANTHROPIC_API_KEY": "sk-ant-x", "SPREADSHEET_ID": "S1"}, creds=object())
    h.rt._llm.check = lambda: (_ for _ in ()).throw(ConnectionResetError("boom"))
    assert h.run("doctor", "--online") == 1 and "ConnectionResetError: boom" in h.text
    assert GOOD_BODY  # keep the shared sample import used


# --------------------------------------------------------------------------- try-draft
class Untouchable:
    """Stands in for Google objects. Any use at all fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"this command must not touch Google (tried .{name})")


def offline_harness(tmp_path, *script, **kwargs):
    llm = FakeLLM(*script)
    return Harness(tmp_path, env={}, llm=llm, sheet=Untouchable(), gmail=Untouchable(), creds=Untouchable(),
                   gspread_client=Untouchable(), **kwargs), llm


def test_try_draft_writes_a_sample_email_and_never_touches_google(tmp_path):
    from tests.samples import GOOD_MODEL_TEXT, GOOD_SUBJECT
    h, llm = offline_harness(tmp_path, GOOD_MODEL_TEXT)
    assert h.run("try-draft") == 0
    assert f"Subject: {GOOD_SUBJECT}" in h.text and "Hi Jane," in h.text and "Personal Instagram: @varunkaveti" in h.text
    assert "Passed every rule check (1 try)" in h.text and "100 tokens in, 200 out" in h.text
    assert "no Google login is needed" in h.text and "with fake (fake-1)" in h.text
    assert "Next: python main.py auth" in h.text and len(llm.calls) == 1


def test_try_draft_uses_the_company_category_and_hook_you_give_it(tmp_path):
    from tests.samples import GOOD_MODEL_TEXT
    h, llm = offline_harness(tmp_path, GOOD_MODEL_TEXT)
    assert h.run("try-draft", "--company", "Nuun", "--category", "electrolytes", "--hook", "their new tablets", "--name", "Sam Lee") == 0
    prompt = llm.calls[0][1]
    assert "Company: Nuun" in prompt and "Category: Hydration" in prompt and "their new tablets" in prompt
    assert "Hi Sam," in h.text and "Sam" not in prompt  # the contact name only ever reaches the greeting


def test_try_draft_show_prompt_prints_exactly_what_is_sent_and_no_contact_data(tmp_path):
    from tests.samples import GOOD_MODEL_TEXT
    h, llm = offline_harness(tmp_path, GOOD_MODEL_TEXT)
    assert h.run("try-draft", "--show-prompt") == 0
    block = next(line for line in h.lines if "exactly what the model is sent" in line)
    assert "SYSTEM:" in block and "USER:" in block and "<lead>" in block and "Company: CeraVe" in block
    assert block.count(llm.calls[0][0]) == 1 and llm.calls[0][1] in block  # the printed text is the real request
    assert "Jane" not in block and "preview@example.com" not in block


def test_try_draft_explains_a_draft_that_breaks_the_rules(tmp_path):
    from tests.samples import model_text
    h, _ = offline_harness(tmp_path, model_text(body="Short and wrong. Celsius is great."))
    assert h.run("try-draft") == 1
    assert "broke these rules after 2 tries" in h.text and "Celsius" in h.text and "required sentence" in h.text
    assert "Its last attempt" in h.text and "Short and wrong." in h.text and "needs tuning" in h.text


def test_try_draft_with_a_rejected_key_prints_one_line_and_exits_1(tmp_path, capsys):
    h, _ = offline_harness(tmp_path, LLMAuthError("Google rejected GEMINI_API_KEY"))
    assert h.run("try-draft") == 1
    assert capsys.readouterr().err.strip() == "Error: Google rejected GEMINI_API_KEY"


def test_try_draft_without_a_key_says_how_to_fix_it(tmp_path, capsys):
    h = Harness(tmp_path, env={}, llm=None, sheet=Untouchable(), gmail=Untouchable(), creds=Untouchable())
    assert h.run("try-draft") == 1 and "ANTHROPIC_API_KEY is empty" in capsys.readouterr().err


def test_try_draft_reports_a_model_refusal_and_a_banned_company(tmp_path):
    from config.errors import LLMRefusal
    h, _ = offline_harness(tmp_path, LLMRefusal("declined"))
    assert h.run("try-draft") == 1 and "did not produce a draft: declined" in h.text
    h2, llm2 = offline_harness(tmp_path, "unused")
    assert h2.run("try-draft", "--company", "Celsius") == 1 and "BANNED_BRANDS" in h2.text and not llm2.calls


def test_try_draft_runs_with_no_env_file_and_no_sheet_setting(tmp_path):
    from tests.samples import GOOD_MODEL_TEXT
    h, _ = offline_harness(tmp_path, GOOD_MODEL_TEXT)
    assert not h.env_path.exists() and h.settings.spreadsheet_id == "" and h.run("try-draft") == 0 and not h.env_path.exists()


# --------------------------------------------------------------------------- configure: privacy note and next step
def good_check(monkeypatch):
    monkeypatch.setattr(C, "build_llm", lambda settings: type("L", (), {"check": lambda self: "key accepted"})())


def test_configure_gemini_explains_the_free_tier_privacy_tradeoff(isolated, monkeypatch):
    good_check(monkeypatch)
    h = Harness(isolated, env={}, read_stdin=lambda: "AIza-fake1")
    assert h.run("configure", "--provider", "gemini", "--key-from-stdin") == 0
    assert "Privacy note" in h.text and "free tier" in h.text and "never contact names or emails" in h.text
    assert "try-draft --show-prompt" in h.text


def test_configure_claude_has_no_gemini_privacy_note(isolated, monkeypatch):
    good_check(monkeypatch)
    h = Harness(isolated, env={}, read_stdin=lambda: "sk-ant-fake1")
    assert h.run("configure", "--provider", "anthropic", "--key-from-stdin") == 0 and "Privacy note" not in h.text


def test_configure_points_to_try_draft_after_a_working_key_and_to_auth_otherwise(isolated, monkeypatch):
    good_check(monkeypatch)
    ok = Harness(isolated, env={}, read_stdin=lambda: "AIza-fake1")
    ok.run("configure", "--provider", "gemini", "--key-from-stdin")
    assert "Next: python main.py try-draft" in ok.text
    unchecked = Harness(isolated, env={}, read_stdin=lambda: "AIza-fake1")
    unchecked.run("configure", "--provider", "gemini", "--key-from-stdin", "--no-check")
    assert "Next: python main.py auth" in unchecked.text and "Next: python main.py try-draft" not in unchecked.text


# --------------------------------------------------------------------------- discover: credits and memory
class Rich(Stub):
    """A lookup backend that can also report a credit balance, like Hunter."""

    def __init__(self, script, balance):
        super().__init__(script)
        self._balance, self.balance_calls = balance, 0

    def balance(self):
        self.balance_calls += 1
        if isinstance(self._balance, Exception):
            raise self._balance
        return self._balance


def credits(remaining, available=50, plan="Free", reset="2026-11-19"):
    return D.Balance(plan, remaining, available, reset)


def test_discover_shows_your_real_hunter_balance(h, brands_csv):
    h.rt._backend = Rich({"Red Bull": found("jo@redbull.com", "redbull.com")}, credits(50))
    assert h.run("discover", "--csv", str(brands_csv)) == 0
    assert "Your Hunter plan (Free): 50 of 50 credits left, resetting 2026-11-19." in h.text
    assert "fewer than" not in h.text and h.rt._backend.balance_calls == 1


def test_discover_warns_when_the_balance_is_below_the_worst_case(h, brands_csv):
    h.rt._backend = Rich({}, credits(4))
    assert h.run("discover", "--csv", str(brands_csv), "--dry-run") == 0  # 3 brands, up to 2 searches each
    assert "4 of 50 credits left" in h.text and "fewer than the 6 this run could use" in h.text


@pytest.mark.parametrize("left", [0, 0.5])
def test_discover_stops_cleanly_when_hunter_has_no_credit_left(h, brands_csv, left):
    backend = Rich({"Red Bull": found("jo@redbull.com", "redbull.com")}, credits(left))
    h.rt._backend = backend
    assert h.run("discover", "--csv", str(brands_csv)) == 1
    assert backend.seen == [] and h.sheet.read_rows() == [] and not h.lookups_path.exists()
    assert "No Hunter credits left" in h.text and "2026-11-19" in h.text and "import --csv" in h.text


def test_a_dry_run_still_previews_when_credits_are_gone(h, brands_csv):
    h.rt._backend = Rich({}, credits(0))
    assert h.run("discover", "--csv", str(brands_csv), "--dry-run") == 0
    assert "would look up: Red Bull" in h.text and "No Hunter credits left" not in h.text


@pytest.mark.parametrize("balance,shown", [(DiscoveryError("Hunter is down"), "Could not read your Hunter balance: Hunter is down"), (None, "")])
def test_discover_still_runs_when_the_balance_cannot_be_read(h, brands_csv, balance, shown):
    h.rt._backend = Rich({"Red Bull": found("jo@redbull.com", "redbull.com")}, balance)
    assert h.run("discover", "--csv", str(brands_csv)) == 0
    assert [r.company for r in h.sheet.read_rows()] == ["Red Bull"] and "Your Hunter plan" not in h.text
    if shown:
        assert shown in h.text


def test_discover_with_apollo_never_asks_for_a_hunter_balance(h, brands_csv):
    backend = Rich({}, credits(0))
    h.rt._backend = backend
    assert h.run("discover", "--csv", str(brands_csv), "--backend", "apollo") == 0
    assert backend.balance_calls == 0 and "Hunter" not in h.text.replace("Hunter or Apollo", "")


def test_brands_with_no_usable_contact_are_not_searched_again_for_30_days(h, brands_csv):
    backend = Rich({"Red Bull": found("jo@redbull.com", "redbull.com")}, credits(50))  # CeraVe and Nuun come back empty
    h.rt._backend = backend
    assert h.run("discover", "--csv", str(brands_csv)) == 0
    saved = json.loads(h.lookups_path.read_text())["dead_ends"]
    assert set(saved) == {"cerave.com", "nuunlife.com"} and saved["cerave.com"] == {"company": "CeraVe", "date": "2026-10-18"}
    assert "not searched again for 30 days" in h.text

    h.lines.clear()
    backend.seen.clear()
    assert h.run("discover", "--csv", str(brands_csv)) == 0
    assert backend.seen == []  # Red Bull is in the sheet; the other two are remembered
    assert "skipping CeraVe: searched on 2026-10-18 with no usable contact; it will be searched again on 2026-11-17" in h.text
    assert "skipping Red Bull: redbull.com is already in the sheet" in h.text


def test_recheck_days_zero_searches_remembered_brands_again(h, brands_csv):
    backend = Rich({}, credits(50))
    h.rt._backend = backend
    h.run("discover", "--csv", str(brands_csv))
    h.lines.clear()
    backend.seen.clear()
    assert h.run("discover", "--csv", str(brands_csv), "--recheck-days", "0") == 0
    assert sorted(backend.seen) == ["CeraVe", "Nuun", "Red Bull"] and "not searched again" not in h.text


def test_a_brand_that_finally_yields_a_contact_is_forgotten(h, brands_csv):
    old = {"version": 1, "dead_ends": {"cerave.com": {"company": "CeraVe", "date": "2026-06-01"}}}
    h.lookups_path.write_text(json.dumps(old))
    h.rt._backend = Rich({"CeraVe": found("al@cerave.com", "cerave.com")}, credits(50))
    assert h.run("discover", "--csv", str(brands_csv)) == 0  # June is outside the 30 days, so CeraVe is searched again
    assert [r.company for r in h.sheet.read_rows()] == ["CeraVe"]
    assert "cerave.com" not in json.loads(h.lookups_path.read_text())["dead_ends"]


def test_lookup_errors_and_quota_stops_are_never_remembered_as_dead_ends(h, brands_csv):
    h.rt._backend = Rich({"Red Bull": DiscoveryError("Red Bull broke"), "CeraVe": QuotaExceeded("plan used up")}, credits(50))
    assert h.run("discover", "--csv", str(brands_csv)) == 1
    assert not h.lookups_path.exists()


def test_a_damaged_lookup_memory_is_replaced_not_fatal(h, brands_csv):
    h.lookups_path.write_text("{ this is not json")
    h.rt._backend = Rich({}, credits(50))
    assert h.run("discover", "--csv", str(brands_csv)) == 0
    assert "every brand counts as not searched yet" in h.text
    assert set(json.loads(h.lookups_path.read_text())["dead_ends"]) == {"redbull.com", "cerave.com", "nuunlife.com"}


def test_a_failure_to_save_the_memory_is_reported_but_not_fatal(h, brands_csv, monkeypatch):
    def refuse(self):
        raise OSError("read-only file system")

    monkeypatch.setattr(D.LookupLog, "save", refuse)
    h.rt._backend = Rich({}, credits(50))
    assert h.run("discover", "--csv", str(brands_csv)) == 0
    assert "Could not save" in h.text and "read-only file system" in h.text and "searched again next time" in h.text


def test_a_dry_run_respects_the_memory_but_never_writes_it(h, brands_csv):
    h.lookups_path.write_text(json.dumps({"version": 1, "dead_ends": {"cerave.com": {"company": "CeraVe", "date": "2026-10-10"}}}))
    before = h.lookups_path.read_text()
    h.rt._backend = Rich({}, credits(50))
    assert h.run("discover", "--csv", str(brands_csv), "--dry-run") == 0
    assert "skipping CeraVe: searched on 2026-10-10" in h.text and "would look up: CeraVe" not in h.text
    assert h.lookups_path.read_text() == before


# --------------------------------------------------------------------------- doctor: Hunter, and Google's new key format
def test_doctor_online_shows_the_hunter_credit_balance(isolated):
    env = {"ANTHROPIC_API_KEY": "sk-ant-x", "SPREADSHEET_ID": "SHEET123", "HUNTER_API_KEY": "hk"}
    h = Harness(isolated, env=env, creds=object(), backend=Rich({}, credits(42, plan="Free")))
    assert h.run("doctor", "--online") == 0
    assert "[ ok ] Hunter key and credits" in h.text and "plan Free: 42 of 50 credits left, resets 2026-11-19" in h.text


def test_doctor_online_reports_a_rejected_hunter_key(isolated):
    env = {"ANTHROPIC_API_KEY": "sk-ant-x", "SPREADSHEET_ID": "SHEET123", "HUNTER_API_KEY": "hk"}
    h = Harness(isolated, env=env, creds=object(), backend=Rich({}, DiscoveryAuthError("Hunter rejected HUNTER_API_KEY")))
    assert h.run("doctor", "--online") == 1 and "[FAIL] Hunter key and credits" in h.text and "Hunter rejected HUNTER_API_KEY" in h.text


def test_doctor_online_accepts_a_hunter_reply_without_a_balance_and_skips_it_without_a_key(isolated):
    env = {"ANTHROPIC_API_KEY": "sk-ant-x", "SPREADSHEET_ID": "SHEET123"}
    keyless = Harness(isolated, env=env, creds=object(), backend=Rich({}, credits(1)))
    keyless.run("doctor", "--online")
    assert "Hunter key and credits" not in keyless.text and keyless.rt._backend.balance_calls == 0
    odd = Harness(isolated, env={**env, "HUNTER_API_KEY": "hk"}, creds=object(), backend=Rich({}, None))
    assert odd.run("doctor", "--online") == 0 and "credit balance was not in the reply" in odd.text


@pytest.mark.parametrize("key,warned", [("AQ.fake-gemini-key-1", False), ("AIzaFakeGeminiKey1", False), ("not-a-google-key", True)])
def test_configure_recognises_both_google_key_formats(isolated, monkeypatch, key, warned):
    good_check(monkeypatch)
    h = Harness(isolated, env={}, read_stdin=lambda: key)
    assert h.run("configure", "--provider", "gemini", "--key-from-stdin") == 0
    assert ("normally start with AIza or AQ." in h.text) is warned


def test_try_draft_points_to_the_next_unfinished_setup_step(tmp_path, monkeypatch):
    from tests.samples import GOOD_MODEL_TEXT
    fresh, _ = offline_harness(tmp_path, GOOD_MODEL_TEXT)
    assert fresh.run("try-draft") == 0 and "Next: python main.py auth" in fresh.text

    token = tmp_path / "token-present.json"
    token.write_text("{}")
    monkeypatch.setattr(C, "TOKEN_FILE", token)
    signed_in, _ = offline_harness(tmp_path, GOOD_MODEL_TEXT)
    assert signed_in.run("try-draft") == 0 and "Next: python main.py init-sheet" in signed_in.text

    ready = Harness(tmp_path, env={"SPREADSHEET_ID": "S1"}, llm=FakeLLM(GOOD_MODEL_TEXT), sheet=Untouchable(), gmail=Untouchable(), creds=Untouchable())
    assert ready.run("try-draft") == 0 and "Next: python main.py status" in ready.text
