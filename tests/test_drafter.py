from datetime import date

import pytest

from config.errors import LLMAuthError, LLMError, LLMRefusal, LLMTransientError
from config.settings import COLUMNS, Col, Sender, Settings, season_context
from src import drafter as D
from src.models import LeadRow
from src.sheet_manager import SheetManager
from tests.fakes import FakeSpreadsheet, FakeWorksheet
from tests.samples import GOOD_BODY, GOOD_MODEL_TEXT, GOOD_SUBJECT, FakeLLM, make_row, model_text, pending

TODAY = date(2026, 10, 18)
COL = {name: i + 1 for i, name in enumerate(COLUMNS)}


def build(rows, *script, **settings_env):
    ws = FakeWorksheet(grid=[list(COLUMNS)] + [list(r) for r in rows])
    sheet = SheetManager(FakeSpreadsheet(ws), ws, sleep=lambda s: None)
    llm = FakeLLM(*script)
    drafter = D.Drafter(Settings.load(env=settings_env), sheet, llm, today=TODAY)
    return drafter, sheet, ws, llm


def cell(ws, row, column):
    return ws.cell(row, COL[column])


# --------------------------------------------------------------------------- prompts
def test_system_prompt_carries_every_rule_and_never_names_a_brand():
    prompt = D.build_system_prompt(Sender(), season_context(TODAY))
    for must in ("top 10", "NCVF", "30 athletes", "fraternities", "cultural associations", "honors college",
                 "wrapping up our fall tournament slate", "We have partnered with brands in the past"):
        assert must in prompt
    assert "Celsius" not in prompt and "Red Bull" not in prompt
    assert "Varun Kaveti" in prompt and "SUBJECT:" in prompt


def test_user_prompt_uses_category_angle_or_the_custom_hook():
    season = season_context(TODAY)
    plain = LeadRow({Col.COMPANY: "CeraVe", Col.CATEGORY: "Skincare"})
    text = D.build_user_prompt(plain, D.pick_angle(plain), season)
    assert "skin barrier health and sun protection" in text and "Use the default angle" in text
    hooked = LeadRow({Col.COMPANY: "CeraVe", Col.CATEGORY: "Skincare", Col.HOOK: "their new SPF launch"})
    text = D.build_user_prompt(hooked, D.pick_angle(hooked), season)
    assert "their new SPF launch" in text and "instead of the default" in text
    assert "<lead>" in text and "</lead>" in text


@pytest.mark.parametrize("category,needle", [
    ("Skincare", "sun protection"), ("Hydration", "electrolytes"), ("Recovery", "recovery between matches"),
    ("Energy", "high-caffeine"), ("Gear", "equipment"), ("energy drink", "high-caffeine"), ("Pet Food", "perform and recover"), ("", "perform and recover"),
])
def test_angles_follow_the_category(category, needle):
    assert needle in D.pick_angle(LeadRow({Col.CATEGORY: category}))


def test_retry_prompt_lists_problems_and_the_previous_draft():
    row = LeadRow({Col.COMPANY: "CeraVe", Col.CATEGORY: "Skincare"})
    text = D.build_user_prompt(row, "angle", season_context(TODAY), problems=["Do not use semicolons."], previous="OLD DRAFT")
    assert "Do not use semicolons." in text and "OLD DRAFT" in text and "corrected email" in text


# --------------------------------------------------------------------------- parsing and assembly
def test_parse_handles_plain_bold_fenced_and_quoted_output():
    assert D.parse_model_output(GOOD_MODEL_TEXT) == (GOOD_SUBJECT, GOOD_BODY)
    fenced = D.parse_model_output('```\n**SUBJECT:** "A title"\n**BODY:**\nOne.\n\nTwo.\n```')
    assert fenced == ("A title", "One.\n\nTwo.")
    inline = D.parse_model_output("subject: Hello\nbody: Text on the same line\nmore text")
    assert inline == ("Hello", "Text on the same line\nmore text")


@pytest.mark.parametrize("bad", ["", "just an email body", "SUBJECT: only a subject", "BODY:\nno subject"])
def test_parse_rejects_malformed_output(bad):
    with pytest.raises(D.DraftFormatError):
        D.parse_model_output(bad)


@pytest.mark.parametrize("name,company,expected", [
    ("Jane Doe", "CeraVe", "Hi Jane,"), ("dr. jane doe", "CeraVe", "Hi Jane,"), ("DOE, JANE", "CeraVe", "Hi Jane,"),
    ("JANE", "CeraVe", "Hi Jane,"), ("Partnerships Team", "CeraVe", "Hi Partnerships Team,"),
    ("Marketing", "CeraVe", "Hi CeraVe team,"), ("J.", "CeraVe", "Hi CeraVe team,"), ("", "CeraVe", "Hi CeraVe team,"),
    ("", "", "Hello,"), ("Mary-Anne Smith", "X", "Hi Mary-Anne,"), ("O'Brien Pat", "X", "Hi O'Brien,"),
])
def test_greetings(name, company, expected):
    assert D.greeting_for(name, company) == expected


def test_assembled_email_has_greeting_optout_signoff_and_both_instagrams():
    email = D.assemble_email(Sender(), "Jane Doe", "CeraVe", GOOD_BODY)
    assert email.startswith("Hi Jane,\n\n") and GOOD_BODY in email
    parts = email.split("\n\n")
    assert parts[-2].startswith("If you'd rather not hear from us")
    assert parts[-1].splitlines() == ["Best,", "Varun Kaveti", "President, Penn State Men's Club Volleyball",
                                     "pennstatemcvb@gmail.com", "Team Instagram: @psumensclubvolleyball", "Personal Instagram: @varunkaveti"]


# --------------------------------------------------------------------------- run: happy path
def test_a_pending_row_is_drafted_and_moves_to_pending_approval():
    drafter, sheet, ws, llm = build([pending("L1")], GOOD_MODEL_TEXT)
    report = drafter.run()
    assert [o.status for o in report.outcomes] == ["drafted"] and report.failed == []
    row = sheet.get_row("L1")
    assert row.approval == "Pending Approval" and row.send_status == "Unsent"
    assert row.subject == GOOD_SUBJECT and row.body.startswith("Hi Jane,\n\n") and "@varunkaveti" in row.body
    assert (report.input_tokens, report.output_tokens) == (100, 200) and len(llm.calls) == 1
    assert llm.calls[0][1].count("CeraVe") >= 1 and "Jane" not in llm.calls[0][1]  # contact name never goes to the model


def test_dry_run_previews_without_touching_the_sheet():
    drafter, sheet, ws, llm = build([pending("L1")], GOOD_MODEL_TEXT)
    before = [list(r) for r in ws.grid]
    report = drafter.run(dry_run=True)
    assert [o.status for o in report.outcomes] == ["preview"] and "Hi Jane" in report.previews[0].email
    assert ws.grid == before and not [c for c in ws.calls if c[0] in ("batch_update", "update", "append_rows")]


def test_only_pending_unsent_rows_are_drafted():
    rows = [pending("L1"), pending("L2", email="b@x.com", **{Col.APPROVAL: "Approved"}), pending("L3", email="c@x.com", **{Col.APPROVAL: "Rejected"}),
            pending("L4", email="d@x.com", **{Col.SEND: "Sent"}), pending("L5", email="e@x.com", **{Col.APPROVAL: "Pending Approval"})]
    drafter, sheet, ws, llm = build(rows, GOOD_MODEL_TEXT)
    report = drafter.run()
    assert [o.row_id for o in report.drafted] == ["L1"] and len(llm.calls) == 1


def test_hand_added_rows_without_ids_or_statuses_are_picked_up():
    row = make_row(**{Col.COMPANY: "Liquid I.V.", Col.TARGET_EMAIL: "pat@liquid-iv.com", Col.CATEGORY: "Hydration"})
    drafter, sheet, ws, llm = build([row], GOOD_MODEL_TEXT)
    report = drafter.run()
    assert len(report.drafted) == 1 and sheet.read_rows()[0].row_id.startswith("L")
    assert "electrolytes" in llm.calls[0][1]


def test_limit_caps_the_run_and_reports_what_is_left():
    rows = [pending(f"L{i}", email=f"p{i}@x.com") for i in range(1, 6)]
    drafter, sheet, ws, llm = build(rows, GOOD_MODEL_TEXT)
    report = drafter.run(limit=2)
    assert len(report.drafted) == 2 and report.remaining == 3 and len(llm.calls) == 2


def test_specific_row_ids_can_be_requested():
    rows = [pending("L1"), pending("L2", email="b@x.com")]
    drafter, sheet, ws, llm = build(rows, GOOD_MODEL_TEXT)
    report = drafter.run(row_ids=["L2", "NOPE"])
    assert [o.row_id for o in report.drafted] == ["L2"] and [o.row_id for o in report.skipped] == ["NOPE"]


# --------------------------------------------------------------------------- run: skips
@pytest.mark.parametrize("extra,reason", [
    ({Col.TARGET_EMAIL: ""}, "Target Email"), ({Col.TARGET_EMAIL: "nope"}, "Target Email"),
    ({Col.COMPANY: "Celsius"}, "BANNED_BRANDS"), ({Col.SUBJECT: "my own subject"}, "already has draft text"),
    ({Col.BODY: "my own body"}, "already has draft text"),
])
def test_unsuitable_rows_are_skipped_with_a_note_and_no_model_call(extra, reason):
    drafter, sheet, ws, llm = build([pending("L1", **extra)], GOOD_MODEL_TEXT)
    report = drafter.run()
    assert [o.status for o in report.outcomes] == ["skipped"] and reason in report.skipped[0].reason
    assert not llm.calls
    assert reason in sheet.get_row("L1").notes


def test_human_text_in_notes_survives_skip_notes():
    drafter, sheet, ws, llm = build([pending("L1", **{Col.TARGET_EMAIL: "", Col.NOTES: "met them at a conference"})], GOOD_MODEL_TEXT)
    drafter.run()
    notes = sheet.get_row("L1").notes
    assert notes.startswith("met them at a conference") and "[draft] skipped" in notes


# --------------------------------------------------------------------------- run: rule enforcement and retry
def test_a_broken_first_draft_is_rewritten_once_with_the_problems_fed_back():
    bad = model_text(body=GOOD_BODY.replace("roster.", "roster; really.") + " Celsius is great.")
    drafter, sheet, ws, llm = build([pending("L1")], bad, GOOD_MODEL_TEXT)
    report = drafter.run()
    assert report.drafted and report.drafted[0].attempts == 2 and len(llm.calls) == 2
    retry_prompt = llm.calls[1][1]
    assert "semicolons" in retry_prompt and "Celsius" in retry_prompt and "Previous draft" in retry_prompt
    assert "Celsius" not in sheet.get_row("L1").body


def test_a_draft_that_keeps_breaking_the_rules_never_reaches_the_sheet():
    bad = model_text(body=GOOD_BODY + " Celsius is great.")
    drafter, sheet, ws, llm = build([pending("L1")], bad)
    report = drafter.run()
    assert report.failed and report.failed[0].attempts == 2 and len(llm.calls) == 2
    row = sheet.get_row("L1")
    assert row.approval == "Pending" and row.subject == "" and row.body == ""
    assert "[draft] draft rejected after 2 tries" in row.notes and "Celsius" in row.notes


def test_a_format_error_is_retried_like_any_other_problem():
    drafter, sheet, ws, llm = build([pending("L1")], "Sure! Here is an email.", GOOD_MODEL_TEXT)
    report = drafter.run()
    assert report.drafted and "exactly this format" in llm.calls[1][1]


def test_model_added_greeting_and_signoff_are_stripped_not_rejected():
    wrapped = model_text(body="Hi Jane,\n\n" + GOOD_BODY + "\n\nBest,\nVarun")
    drafter, sheet, ws, llm = build([pending("L1")], wrapped)
    report = drafter.run()
    assert report.drafted and report.drafted[0].attempts == 1
    assert sheet.get_row("L1").body.count("Hi Jane") == 1


def test_dashes_from_the_model_are_repaired_automatically():
    dashed = model_text(body=GOOD_BODY.replace("roster.", f"roster {chr(0x2014)} really."))
    drafter, sheet, ws, llm = build([pending("L1")], dashed)
    report = drafter.run()
    assert report.drafted and report.drafted[0].attempts == 1 and chr(0x2014) not in sheet.get_row("L1").body


def test_successful_redraft_clears_the_old_rejection_note_but_keeps_human_notes():
    note = "call first\n[draft] draft rejected after 2 tries: something"
    drafter, sheet, ws, llm = build([pending("L1", **{Col.NOTES: note})], GOOD_MODEL_TEXT)
    drafter.run()
    assert sheet.get_row("L1").notes == "call first"


# --------------------------------------------------------------------------- run: model failures
def test_a_refusal_fails_that_row_only():
    rows = [pending("L1"), pending("L2", email="b@x.com")]
    drafter, sheet, ws, llm = build(rows, LLMRefusal("declined"), GOOD_MODEL_TEXT)
    report = drafter.run()
    assert [o.status for o in report.outcomes] == ["failed", "drafted"]
    assert "the model declined" in sheet.get_row("L1").notes


def test_other_model_errors_fail_the_row_and_continue():
    rows = [pending("L1"), pending("L2", email="b@x.com")]
    drafter, sheet, ws, llm = build(rows, LLMError("ran out of room"), GOOD_MODEL_TEXT)
    report = drafter.run()
    assert [o.status for o in report.outcomes] == ["failed", "drafted"] and "model error" in sheet.get_row("L1").notes


def test_bad_key_stops_the_run_immediately():
    rows = [pending(f"L{i}", email=f"p{i}@x.com") for i in range(1, 4)]
    drafter, sheet, ws, llm = build(rows, LLMAuthError("bad key"))
    report = drafter.run()
    assert report.aborted == "bad key" and len(llm.calls) == 1 and len(report.outcomes) == 1


def test_three_outages_in_a_row_stop_the_run_but_one_blip_does_not():
    rows = [pending(f"L{i}", email=f"p{i}@x.com") for i in range(1, 6)]
    drafter, sheet, ws, llm = build(rows, *([LLMTransientError("busy")] * 3), GOOD_MODEL_TEXT)
    report = drafter.run()
    assert report.aborted and len(llm.calls) == 3 and len(report.failed) == 3

    drafter, sheet, ws, llm = build(rows[:3], LLMTransientError("busy"), GOOD_MODEL_TEXT)
    report = drafter.run()
    assert not report.aborted and [o.status for o in report.outcomes] == ["failed", "drafted", "drafted"]


# --------------------------------------------------------------------------- run: humans editing during a run
def test_a_row_edited_while_drafting_is_not_overwritten():
    drafter, sheet, ws, llm = build([pending("L1")], GOOD_MODEL_TEXT)

    original = llm.generate

    def generate_then_edit(system, prompt):
        result = original(system, prompt)
        ws._set(2, COL[Col.SUBJECT], "the human got there first")
        return result

    llm.generate = generate_then_edit
    report = drafter.run()
    assert report.outcomes[0].status == "skipped" and "edited while drafting" in report.outcomes[0].reason
    assert cell(ws, 2, Col.SUBJECT) == "the human got there first" and cell(ws, 2, Col.BODY) == ""


def test_a_row_deleted_while_drafting_is_skipped_cleanly():
    drafter, sheet, ws, llm = build([pending("L1"), pending("L2", email="b@x.com")], GOOD_MODEL_TEXT)
    original = llm.generate

    def generate_then_delete(system, prompt):
        result = original(system, prompt)
        ws.grid[1] = [""] * len(COLUMNS)
        return result

    llm.generate = generate_then_delete
    report = drafter.run(limit=1)
    assert report.outcomes[0].status == "skipped" and not report.drafted
