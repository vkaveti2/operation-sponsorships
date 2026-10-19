import stat
from datetime import timedelta

import pytest

import src.cli as C
from config.errors import DiscoveryAuthError, LLMAuthError, QuotaExceeded
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
