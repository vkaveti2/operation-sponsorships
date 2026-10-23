import pytest

from src import guardrails as g
from tests.samples import GOOD_BODY, GOOD_SUBJECT

EM, EN = chr(0x2014), chr(0x2013)
KW = dict(banned_brands=["Celsius"], season_key="late_fall", context=["CeraVe", "Skincare"])


def check(subject=GOOD_SUBJECT, body=GOOD_BODY, **over):
    return g.check_draft(subject, body, **{**KW, **over})


def has(problems, needle):
    return any(needle.lower() in p.lower() for p in problems)


def test_the_reference_draft_is_clean():
    assert check() == []


def sub(old, new, text=GOOD_BODY):
    """Replace that fails loudly if the anchor is missing, so a typo can never make a test vacuous."""
    assert old in text, f"test bug: anchor not found: {old[:50]!r}"
    return text.replace(old, new)


REQ_LINE = "We have worked with brands before and it has always been pretty informal, just getting product in the hands of athletes who are genuinely using it."
RULE_CASES = [
    ("required line missing", sub(REQ_LINE, "We love brands."), "required sentence"),
    ("required line reworded", sub("genuinely using it", "using it"), "required sentence"),
    ("top 10 missing", sub("top 10 nationally ranked", "nationally ranked"), "top 10"),
    ("NCVF missing", sub("National Collegiate Volleyball Federation (NCVF)", "a national league"), "NCVF"),
    ("roster missing", sub("Our 30 athletes", "Our athletes"), "30 athletes"),
    ("campus reach missing", sub("in fraternities, cultural associations, the honors college, and other club sports teams", "around campus"), "campus reach"),
    ("season missing", sub("We are wrapping up our fall tournament slate and gearing up for our full spring competitive run. ", ""), "season"),
    ("ask missing", GOOD_BODY.rsplit("\n\n", 1)[0] + "\n\nLet me know if you have any questions.", "ask"),
    ("banned brand", GOOD_BODY + " Celsius is great.", "Celsius"),
    ("banned brand other case", GOOD_BODY + " We love celsius.", "Celsius"),
    ("semicolon", sub("roster.", "roster; really."), "semicolons"),
    ("em dash", sub("roster.", f"roster {EM} really."), "dashes"),
    ("en dash", sub("roster.", f"roster {EN} really."), "dashes"),
    ("spaced hyphen", sub("roster.", "roster - really."), "dashes"),
    ("exclamation", sub("roster.", "roster!"), "exclamation"),
    ("ellipsis", sub("roster.", "roster..."), "ellipses"),
    ("emoji", GOOD_BODY + " " + chr(0x1F3D0), "emoji"),
    ("markdown bold", sub("top 10", "**top 10**"), "plain text"),
    ("bullets", GOOD_BODY + "\n- extra point", "plain text"),
    ("placeholder", "[First Name] " + GOOD_BODY, "placeholder"),
    ("fluff", sub("Tournament weekends", "I hope this finds you well. Tournament weekends"), "stock phrases"),
    ("greeting", "Hi Jane,\n" + GOOD_BODY, "greeting"),
    ("shouting 6 letters", sub("long days", "LONGER days"), "ALL CAPS"),
    ("shouting 4 letters", sub("long days", "FREE days"), "ALL CAPS"),
    ("invented number", sub("long days", "long days and 5000 followers"), "numbers"),
    ("too short", "Top 10 NCVF.", "words"),
]


@pytest.mark.parametrize("body,needle", [c[1:] for c in RULE_CASES], ids=[c[0] for c in RULE_CASES])
def test_each_rule_fires(body, needle):
    assert has(check(body=body), needle), check(body=body)


def test_acronyms_we_use_are_not_shouting():
    body = GOOD_BODY + " We also know PSU, NCAA and USAV."
    assert not has(check(body=body), "ALL CAPS")


def test_length_ceiling():
    long = GOOD_BODY + " " + "extra " * 80
    assert has(check(body=long), "Cut it to")


@pytest.mark.parametrize("subject,needle", [
    ("", "empty"), ("x" * 71, "characters"), ("Re: hello", "Re:"), ("Big news!", "exclamation"), ("HUGE partnership", "ALL CAPS"),
], ids=["empty", "too-long", "re-prefix", "exclamation", "shouting"])
def test_subject_rules(subject, needle):
    assert has(check(subject=subject), needle)


def test_names_given_to_the_model_are_exempt_from_caps_and_number_rules():
    body = GOOD_BODY.replace("long days", "long days with SPF 50 in the NIVEA routine")
    assert has(check(body=body), "ALL CAPS") and has(check(body=body), "numbers")
    ok = check(body=body, context=["NIVEA", "SPF 50 sunscreen"])
    assert not has(ok, "ALL CAPS") and not has(ok, "numbers")


def test_season_rules_follow_the_calendar_key():
    spring_body = GOOD_BODY.replace("We are wrapping up our fall tournament slate and gearing up for our full spring competitive run.",
                                    "We are deep in our spring season with regionals and nationals ahead.")
    assert has(check(body=spring_body), "season")  # late_fall wants fall and spring
    assert not has(check(body=spring_body, season_key="spring"), "season")
    off = GOOD_BODY.replace("We are wrapping up our fall tournament slate and gearing up for our full spring competitive run.",
                            "We are already planning for next season.")
    assert not has(check(body=off, season_key="offseason"), "season")


def test_reach_needs_two_of_four_groups_missing_to_fail():
    one_missing = GOOD_BODY.replace("the honors college, and other club sports teams", "and other club sports teams")
    assert not has(check(body=one_missing), "campus reach")


def test_tidy_text_cases():
    curly, nbh = chr(0x2019), chr(0x2011)
    assert g.tidy_text(f"Hi {EM} we{curly}re top{nbh}10 {EN} really  5{EN}7 days -- ok") == "Hi, we're top-10, really 5-7 days, ok"
    assert g.tidy_text("One.\n\n\n\nTwo.  \r\nThree") == "One.\n\nTwo.\nThree"
    assert g.tidy_text(f"Hello\n\n{EM} Varun").count("\n\n") == 1


def test_strip_wrapper_removes_only_whole_line_greeting_and_signoff():
    assert g.strip_wrapper("Hi Jane,\n\nReal text.\nMore.\n\nBest,\nVarun\nPresident") == "Real text.\nMore."
    assert g.strip_wrapper("Hi Jane, inline stays.\n\nThanks for your time.").startswith("Hi Jane, inline")
    assert g.strip_wrapper("Body.\n\nThank you.") == "Body."
    assert g.strip_wrapper("Body only.") == "Body only."


def test_placeholder_detection_ignores_links_and_addresses():
    assert g.placeholder_tokens("Hi [First Name], <jane@x.com> <https://x.com> {company}") == ["[First Name]", "{company}"]
    assert g.placeholder_tokens("we can insert your product") == []
    assert g.placeholder_tokens("INSERT HERE") == ["INSERT"]


def test_banned_brand_matches_whole_words_only():
    assert g.find_banned("Celsius, celsius! and CELSIUSX", ["Celsius"]) == ["Celsius"]
    assert g.find_banned("nothing here", ["Celsius", ""]) == []


GOOD_SEND = dict(subject="Hello there", body="word " * 25, to_email="jane@cerave.com", banned_brands=["Celsius"])


def test_lint_for_send_passes_a_normal_email():
    assert g.lint_for_send(**GOOD_SEND) == []


@pytest.mark.parametrize("over,needle", [
    ({"to_email": "nope"}, "valid email"), ({"subject": " "}, "subject is empty"), ({"subject": "a\nb"}, "line break"),
    ({"subject": "x" * 201}, "longer than 200"), ({"body": ""}, "body is empty"), ({"body": "too short"}, "under 20 words"),
    ({"body": "word " * 25 + "[First Name]"}, "placeholder"), ({"body": "word " * 25 + "Celsius"}, "brand"),
])
def test_lint_for_send_catches_mistakes(over, needle):
    problems = g.lint_for_send(**{**GOOD_SEND, **over})
    assert any(needle in p for p in problems), problems


def test_lint_does_not_police_style_a_human_may_have_chosen():
    edited = "Hey Jane! Great to meet you; here are 5 reasons " + EM + " " + "word " * 25
    assert g.lint_for_send(**{**GOOD_SEND, "body": edited}) == []
