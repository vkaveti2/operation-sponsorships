import json
from datetime import date

import pytest
import requests

from config.errors import DiscoveryAuthError, DiscoveryError, QuotaExceeded
from config.settings import COLUMNS, TARGET_TITLES, Col
from src import discovery as D
from src.models import Lead, LeadRow
from tests.fakes import FakeGspreadClient, FakeSpreadsheet, FakeWorksheet


# --------------------------------------------------------------------------- parsing targets
def parse(rows, **kw):
    return D.parse_target_rows(rows, **kw)


def test_company_and_domain_rows_become_lookups():
    r = parse([["Company Name", "Domain", "Category"], ["Red Bull", "https://www.redbull.com/us", "energy drink"], ["CeraVe", "cerave.com", "Skin Care"]])
    assert [(t.company, t.domain, t.category) for t in r.lookups] == [("Red Bull", "redbull.com", "Energy"), ("CeraVe", "cerave.com", "Skincare")]
    assert r.contacts == [] and not any("category" in msg for _, msg in r.issues)


def test_rows_with_an_email_are_imported_as_is():
    r = parse([["Company", "Target Email", "Target Name", "Category", "Custom Hook / Angle"], ["KT Tape", " Pat@KTTape.com ", "Pat Lee", "tape", "taped wrists"]])
    assert r.lookups == [] and len(r.contacts) == 1
    lead = r.contacts[0]
    assert (lead.company, lead.email, lead.target_name, lead.category, lead.hook, lead.domain) == ("KT Tape", "pat@kttape.com", "Pat Lee", "Recovery", "taped wrists", "kttape.com")


def test_mixed_rows_split_correctly_and_header_aliases_work():
    r = parse([["Brand", "Website", "Email Address"], ["A", "a.com", ""], ["B", "", "b@b.com"], ["C", "c.com", "c@c.com"]])
    assert [t.company for t in r.lookups] == ["A"] and [c.company for c in r.contacts] == ["B", "C"]


@pytest.mark.parametrize("rows,needle", [
    ([], "empty"), ([["", ""]], "empty"),
    ([["Foo", "Bar"], ["x", "y"]], "No company column"),
    ([["Company", "Category"], ["Red Bull", "Energy"]], "Domain column"),
])
def test_unusable_files_explain_what_is_needed(rows, needle):
    with pytest.raises(DiscoveryError, match=needle):
        parse(rows)


def test_row_level_problems_are_reported_not_fatal():
    r = parse([["Company", "Domain", "Target Email", "Category"],
               ["", "x.com", "", "Energy"],                              # no company
               ["Celsius", "celsius.com", "", "Energy"],                 # banned
               ["BadMail", "b.com", "not-an-email", "Energy"],           # invalid email
               ["Dupe", "d.com", "", "Energy"], ["Dupe again", "d.com", "", "Energy"],
               ["Mystery", "m.com", "", "Pet Food"], ["Uncat", "u.com", "", ""],
               ["P1", "p.com", "same@p.com", "Gear"], ["P2", "p2.com", "SAME@p.com", "Gear"],
               ["BadDomain", "nope", "", "Gear"]], banned_brands=["Celsius"])
    messages = " | ".join(m for _, m in r.issues)
    for needle in ("no company name", "BANNED_BRANDS", "not a valid email", "appears twice", "unrecognised category 'Pet Food'", "no category", "not a valid domain"):
        assert needle in messages, needle
    assert {t.company for t in r.lookups} == {"Dupe", "Mystery", "Uncat", "BadDomain"}
    assert [c.company for c in r.contacts] == ["P1"]
    assert next(t for t in r.lookups if t.company == "Mystery").category == "Pet Food"


def test_blank_rows_and_leading_blank_lines_are_ignored():
    r = parse([["", ""], ["Company", "Domain"], ["", ""], ["A", "a.com"]])
    assert [t.company for t in r.lookups] == ["A"]


def test_a_bare_name_column_means_the_brand():
    r = parse([["Name", "Domain"], ["Nuun", "nuunlife.com"]])
    assert r.lookups[0].company == "Nuun"


def test_csv_reading_handles_bom_semicolons_cp1252_and_missing_files(tmp_path):
    bom = tmp_path / "bom.csv"
    bom.write_bytes((chr(0xFEFF) + "Company Name,Domain,Category\nRed Bull,redbull.com,Energy\n").encode("utf-8"))
    assert D.read_targets_csv(bom).lookups[0].company == "Red Bull"
    semi = tmp_path / "semi.csv"
    semi.write_text("Company Name;Domain;Category\nCeraVe;cerave.com;Skincare\n")
    assert D.read_targets_csv(semi).lookups[0].domain == "cerave.com"
    legacy = tmp_path / "legacy.csv"
    legacy.write_bytes(f"Company Name,Domain,Category\nCaf{chr(0xE9)} Brand,cafe.com,Energy\n".encode("cp1252"))
    assert D.read_targets_csv(legacy).lookups[0].company == f"Caf{chr(0xE9)} Brand"
    with pytest.raises(DiscoveryError, match="targets.example.csv"):
        D.read_targets_csv(tmp_path / "missing.csv")


def test_the_shipped_example_list_is_valid_and_ready():
    r = D.read_targets_csv("data/targets.example.csv", banned_brands=["Celsius"])
    assert len(r.lookups) == 20 and r.contacts == [] and r.issues == []
    assert {t.category for t in r.lookups} == {"Energy", "Skincare", "Hydration", "Recovery", "Gear"}
    assert "Red Bull" in {t.company for t in r.lookups} and "Celsius" not in {t.company for t in r.lookups}


def test_reading_targets_from_another_google_sheet():
    ws = FakeWorksheet(title="Targets", grid=[["Company Name", "Domain", "Category"], ["Liquid I.V.", "liquid-iv.com", "Hydration"]])
    client = FakeGspreadClient(FakeSpreadsheet(ws, sheet_id="ABC123"))
    r = D.read_targets_sheet(client, "https://docs.google.com/spreadsheets/d/ABC123/edit#gid=0", "Targets")
    assert r.lookups[0].company == "Liquid I.V."
    assert D.read_targets_sheet(client, "ABC123").lookups[0].domain == "liquid-iv.com"  # first tab when none is named
    with pytest.raises(DiscoveryError, match="no tab called 'Nope'"):
        D.read_targets_sheet(client, "ABC123", "Nope")
    with pytest.raises(DiscoveryError, match="could not find that sheet"):
        D.read_targets_sheet(client, "WRONG")
    with pytest.raises(DiscoveryError, match="URL or ID"):
        D.read_targets_sheet(client, "  ")


def test_domains_already_in_the_sheet():
    rows = [LeadRow({Col.DOMAIN: "redbull.com", Col.TARGET_EMAIL: "a@redbull.com"}), LeadRow({Col.TARGET_EMAIL: "b@cerave.com"}), LeadRow({})]
    assert D.sheet_domains(rows) == {"redbull.com", "cerave.com"}


# --------------------------------------------------------------------------- ranking
@pytest.mark.parametrize("title,expect", [
    ("Campus Marketing Manager", "target"), ("Field Marketing Manager", "target"), ("Sports Marketing Specialist", "target"),
    ("Grassroots Marketing Coordinator", "target"), ("Partnerships Manager", "target"), ("Senior Field Marketing Manager", "target"),
    ("Campus Marketing Coordinator", "target"), ("Partnerships Coordinator", "target"),
    ("Head of Athlete Partnerships", "good"), ("Community Manager", "ok"), ("Brand Marketing Manager", "good"), ("Marketing Manager", "ok"),
    ("Director of Marketing", "weak"), ("VP of Marketing", "no"), ("Chief Marketing Officer", "no"), ("Marketing Intern", "no"),
    ("Social Media Manager", "no"), ("Software Engineer", "no"), ("Customer Support Manager", "no"), ("", "no"),
])
def test_title_scores_fall_in_the_right_band(title, expect):
    score = D.title_score(title)
    band = {"target": score >= 90, "good": 35 <= score < 90, "ok": 20 <= score < 35, "weak": D.MIN_TITLE_SCORE <= score < 20, "no": score < D.MIN_TITLE_SCORE}
    assert band[expect], (title, score)


def test_title_ordering_prefers_exact_targets_over_senior_or_generic_roles():
    order = ["Partnerships Manager", "Brand Marketing Manager", "Marketing Manager", "Director of Marketing", "VP of Marketing"]
    scores = [D.title_score(t) for t in order]
    assert scores == sorted(scores, reverse=True)


def c(email, position="Partnerships Manager", confidence=90, first="Jo", last="Ng", status="valid"):
    return D.Contact(email, first, last, position, confidence, status, "Hunter")


def test_rank_drops_generic_low_confidence_and_irrelevant_people():
    ranked = D.rank_contacts([
        c("info@x.com"), c("partnerships@x.com", position="Partnerships Manager"),  # a partnerships inbox is fine
        c("low@x.com", confidence=30), c("eng@x.com", position="Software Engineer"), c("nopos@x.com", position=""),
        c("vp@x.com", position="VP of Marketing"), c("jo@x.com"),
    ], min_confidence=50, keep=5)
    assert [r.email for r in ranked] == ["jo@x.com", "partnerships@x.com"]


def test_rank_orders_by_fit_then_confidence_dedupes_and_keeps_n():
    ranked = D.rank_contacts([
        c("a@x.com", "Marketing Manager", 99), c("b@x.com", "Field Marketing Manager", 60), c("c@x.com", "Field Marketing Manager", 95),
        c("B@x.com", "Field Marketing Manager", 99), c("d@x.com", "Brand Marketing Manager", 90),
    ], min_confidence=50, keep=3)
    assert [r.email for r in ranked] == ["b@x.com", "c@x.com", "d@x.com"] and ranked[0].confidence == 99


def test_contact_names_are_tidied():
    assert D.Contact("a@x.com", "JANE", "DOE").full_name == "Jane Doe"
    assert D.Contact("a@x.com", "Jane", "McDoe").full_name == "Jane McDoe" and D.Contact("a@x.com", "", "").full_name == ""


# --------------------------------------------------------------------------- fake HTTP session
class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Session:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def _next(self, kind, url, kwargs):
        self.calls.append((kind, url, kwargs))
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)


def hunter_body(*people, domain="x.com", org="X Corp"):
    emails = [{"value": e, "type": t, "confidence": conf, "first_name": f, "last_name": l, "position": pos, "verification": {"status": v}}
              for (e, f, l, pos, conf, t, v) in people]
    return {"data": {"domain": domain, "organization": org, "emails": emails}}


PERSON = ("jo@x.com", "Jo", "Ng", "Partnerships Manager", 92, "personal", "valid")
NO_SLEEP = dict(sleep=lambda s: None)


# --------------------------------------------------------------------------- Hunter
def test_hunter_request_puts_the_key_in_a_header_and_never_in_the_url():
    session = Session(Resp(200, hunter_body(PERSON)))
    D.HunterBackend("SECRET-KEY", session=session, **NO_SLEEP).find_contacts(D.Target("X", "x.com"))
    kind, url, kw = session.calls[0]
    assert kind == "GET" and url == "https://api.hunter.io/v2/domain-search"
    assert kw["headers"]["X-API-KEY"] == "SECRET-KEY" and "SECRET-KEY" not in json.dumps(kw["params"]) and "api_key" not in kw["params"]
    assert kw["params"]["domain"] == "x.com" and kw["params"]["limit"] == 10 and kw["params"]["type"] == "personal"
    assert kw["params"]["department"] == "marketing,communication" and kw["params"]["job_titles"] == ",".join(TARGET_TITLES)
    assert kw["timeout"] == D.REQUEST_TIMEOUT


def test_hunter_uses_the_company_name_when_there_is_no_domain():
    session = Session(Resp(200, hunter_body(PERSON)))
    D.HunterBackend("k", session=session, **NO_SLEEP).find_contacts(D.Target("Red Bull"))
    params = session.calls[0][2]["params"]
    assert params["company"] == "Red Bull" and "domain" not in params


def test_hunter_returns_ranked_title_matches_from_the_first_search_only():
    other = ("al@x.com", "Al", "Bo", "Marketing Manager", 80, "personal", "valid")
    session = Session(Resp(200, hunter_body(other, PERSON)))
    result = D.HunterBackend("k", session=session, **NO_SLEEP).find_contacts(D.Target("X", "x.com"), per_domain=1)
    assert [c.email for c in result.contacts] == ["jo@x.com"] and result.searches == 1 and len(session.calls) == 1
    assert result.domain == "x.com" and result.organization == "X Corp" and "target job title" in result.note


def test_hunter_falls_back_to_a_broader_search_when_no_title_matches():
    session = Session(Resp(200, hunter_body()), Resp(200, hunter_body(("al@x.com", "Al", "Bo", "Brand Marketing Manager", 80, "personal", "valid"))))
    result = D.HunterBackend("k", session=session, **NO_SLEEP).find_contacts(D.Target("X", "x.com"))
    assert [c.email for c in result.contacts] == ["al@x.com"] and result.searches == 2 and "no exact title" in result.note
    assert "job_titles" in session.calls[0][2]["params"] and "job_titles" not in session.calls[1][2]["params"]


def test_hunter_broad_search_can_be_turned_off_and_empty_results_say_so():
    session = Session(Resp(200, hunter_body()))
    result = D.HunterBackend("k", session=session, **NO_SLEEP).find_contacts(D.Target("X", "x.com"), broad=False)
    assert result.contacts == [] and result.searches == 1 and len(session.calls) == 1 and "no usable" in result.note


def test_hunter_skips_generic_invalid_and_unreadable_entries():
    body = hunter_body(
        ("info@x.com", "", "", "", 99, "generic", "valid"), ("bad@x.com", "Bo", "Ng", "Partnerships Manager", 90, "personal", "invalid"),
        ("weird@x.com", "We", "Ird", "Partnerships Manager", "high", "personal", "valid"), PERSON)
    body["data"]["emails"].append("garbage")
    result = D.HunterBackend("k", session=Session(Resp(200, body)), **NO_SLEEP).find_contacts(D.Target("X", "x.com"))
    assert [c.email for c in result.contacts] == ["jo@x.com"]


def test_hunter_unknown_domain_is_not_an_error():
    body = {"data": {"domain": None, "organization": None, "emails": []}, "meta": {"results": 0}}
    result = D.HunterBackend("k", session=Session(Resp(200, body)), **NO_SLEEP).find_contacts(D.Target("X", "x.com"))
    assert result.contacts == [] and result.domain == ""


@pytest.mark.parametrize("status,exc,needle", [
    (401, DiscoveryAuthError, "HUNTER_API_KEY"), (429, QuotaExceeded, "no searches left"),
    (400, DiscoveryError, "missing a required parameter"),
])
def test_hunter_status_codes_map_to_clear_errors(status, exc, needle):
    payload = {"errors": [{"id": "wrong_params", "code": status, "details": "You are missing a required parameter: domain or company"}]}
    with pytest.raises(exc, match=needle):
        D.HunterBackend("k", session=Session(Resp(status, payload)), **NO_SLEEP).find_contacts(D.Target("X", "x.com"))


def test_hunter_retries_rate_limits_and_server_errors_then_succeeds():
    sleeps = []
    session = Session(Resp(403, {"errors": []}), Resp(503), Resp(200, hunter_body(PERSON)))
    result = D.HunterBackend("k", session=session, sleep=sleeps.append).find_contacts(D.Target("X", "x.com"))
    assert result.contacts and sleeps[:2] == [2, 6] and len(session.calls) == 3


def test_hunter_gives_up_after_the_retries():
    for status, needle in ((403, "rate limiting"), (500, "server problem")):
        with pytest.raises(DiscoveryError, match=needle):
            D.HunterBackend("k", session=Session(Resp(status, {"errors": []})), **NO_SLEEP).find_contacts(D.Target("X", "x.com"))
    with pytest.raises(DiscoveryError, match="internet"):
        D.HunterBackend("k", session=Session(requests.exceptions.ConnectionError("down")), **NO_SLEEP).find_contacts(D.Target("X", "x.com"))
    with pytest.raises(DiscoveryError, match="could not be read"):
        D.HunterBackend("k", session=Session(Resp(200, None)), **NO_SLEEP).find_contacts(D.Target("X", "x.com"))


# --------------------------------------------------------------------------- Apollo
def apollo_people(*specs):
    return {"total_entries": len(specs), "people": [{"id": i, "first_name": f, "title": t, "has_email": h} for (i, f, t, h) in specs]}


def apollo_match(email, status="verified", first="Jo", last="Ng", title="Partnerships Manager", org="X Corp"):
    return {"person": {"email": email, "email_status": status, "first_name": first, "last_name": last, "title": title, "organization": {"name": org}}}


def test_apollo_searches_then_reveals_only_the_best_people():
    people = apollo_people(("p1", "Jo", "Partnerships Manager", True), ("p2", "Al", "Marketing Manager", True), ("p3", "Cy", "Field Marketing Manager", True),
                           ("p4", "Di", "Partnerships Manager", False), ("p5", "Ed", "Software Engineer", True))
    session = Session(Resp(200, people), Resp(200, apollo_match("jo@x.com")), Resp(200, apollo_match("cy@x.com", first="Cy", title="Field Marketing Manager")))
    backend = D.ApolloBackend("APOLLO-KEY", session=session, **NO_SLEEP)
    result = backend.find_contacts(D.Target("X", "x.com"), per_domain=2)
    assert sorted(c.email for c in result.contacts) == ["cy@x.com", "jo@x.com"] and result.searches == 2 and backend.reveals == 2
    search = session.calls[0]
    assert search[1] == D.ApolloBackend.SEARCH_URL and search[2]["headers"]["x-api-key"] == "APOLLO-KEY" and "APOLLO-KEY" not in json.dumps(search[2]["params"])
    assert search[2]["params"]["person_titles[]"] == TARGET_TITLES and search[2]["params"]["q_organization_domains_list[]"] == ["x.com"]
    reveal_ids = [call[2]["params"]["id"] for call in session.calls[1:]]
    assert reveal_ids == ["p1", "p3"] and all(call[1] == D.ApolloBackend.MATCH_URL for call in session.calls[1:])
    assert session.calls[1][2]["params"]["reveal_personal_emails"] == "false" and session.calls[1][2]["params"]["reveal_phone_number"] == "false"


def test_apollo_skips_unusable_revealed_emails():
    people = apollo_people(("p1", "A", "Partnerships Manager", True), ("p2", "B", "Partnerships Manager", True), ("p3", "C", "Partnerships Manager", True))
    session = Session(Resp(200, people), Resp(200, apollo_match("email_not_unlocked@domain.com")), Resp(200, apollo_match("bad@x.com", status="invalid")), Resp(200, {"person": None}))
    result = D.ApolloBackend("k", session=session, **NO_SLEEP).find_contacts(D.Target("X", "x.com"), per_domain=3)
    assert result.contacts == [] and "no usable" in result.note


def test_apollo_reveal_cap_stops_the_batch_and_keeps_earlier_finds():
    people = apollo_people(("p1", "A", "Partnerships Manager", True))
    backend = D.ApolloBackend("k", session=Session(Resp(200, people), Resp(200, apollo_match("a@x.com")), Resp(200, people)), reveal_cap=1, **NO_SLEEP)
    first = backend.find_contacts(D.Target("X", "x.com"))
    assert first.contacts and first.stop == ""
    second = backend.find_contacts(D.Target("Y", "y.com"))
    assert second.contacts == [] and "--max-reveals" in second.stop and backend.reveals == 1


def test_apollo_keeps_the_people_it_already_paid_for_when_the_cap_hits_mid_brand():
    people = apollo_people(("p1", "Jo", "Partnerships Manager", True), ("p2", "Cy", "Field Marketing Manager", True))
    session = Session(Resp(200, people), Resp(200, apollo_match("jo@x.com")))
    backend = D.ApolloBackend("k", session=session, reveal_cap=1, **NO_SLEEP)
    result = backend.find_contacts(D.Target("X", "x.com"), per_domain=2)
    assert [c.email for c in result.contacts] == ["jo@x.com"] and "--max-reveals" in result.stop
    assert len([c for c in session.calls if c[1] == D.ApolloBackend.MATCH_URL]) == 1  # no second paid lookup


def test_apollo_provider_quota_mid_brand_also_keeps_earlier_finds():
    people = apollo_people(("p1", "Jo", "Partnerships Manager", True), ("p2", "Cy", "Field Marketing Manager", True))
    session = Session(Resp(200, people), Resp(200, apollo_match("jo@x.com")), Resp(429, {"message": "limit"}))
    result = D.ApolloBackend("k", session=session, **NO_SLEEP).find_contacts(D.Target("X", "x.com"), per_domain=2)
    assert [c.email for c in result.contacts] == ["jo@x.com"] and "rate limiting" in result.stop


def test_apollo_needs_a_domain_and_maps_errors():
    with pytest.raises(DiscoveryError, match="need a Domain"):
        D.ApolloBackend("k", session=Session(Resp(200, {})), **NO_SLEEP).find_contacts(D.Target("X"))
    for status, exc, needle in ((401, DiscoveryAuthError, "APOLLO_API_KEY"), (403, DiscoveryAuthError, "master API key"), (429, QuotaExceeded, "rate limiting"), (500, DiscoveryError, "server problem"), (422, DiscoveryError, "422")):
        with pytest.raises(exc, match=needle):
            D.ApolloBackend("k", session=Session(Resp(status, {"message": "x"})), **NO_SLEEP).find_contacts(D.Target("X", "x.com"))
    with pytest.raises(DiscoveryError, match="may or may not have used a credit"):
        D.ApolloBackend("k", session=Session(requests.exceptions.ReadTimeout()), **NO_SLEEP).find_contacts(D.Target("X", "x.com"))


def test_backend_factory_and_keys():
    assert isinstance(D.build_backend("hunter", "k"), D.HunterBackend) and isinstance(D.build_backend("apollo", "k", reveal_cap=3), D.ApolloBackend)
    assert D.build_backend("apollo", "k", reveal_cap=3).reveal_cap == 3


# --------------------------------------------------------------------------- running a batch
class StubBackend:
    name = "Stub"

    def __init__(self, script):
        self.script, self.seen = dict(script), []

    def find_contacts(self, target, **kw):
        self.seen.append(target.company)
        item = self.script.get(target.company, D.LookupResult())
        if isinstance(item, Exception):
            raise item
        return item


def found(*emails, domain="", searches=1):
    return D.LookupResult([D.Contact(e, "Jo", "Ng", "Partnerships Manager", 90, "valid", "Stub") for e in emails], domain, "", searches)


T = lambda name, domain="", cat="Energy": D.Target(name, domain or f"{name.lower()}.com", cat)  # noqa: E731


def test_discover_builds_leads_with_the_found_note_and_target_fields():
    backend = StubBackend({"A": found("jo@a.com", domain="a.com")})
    report = D.discover([D.Target("A", "a.com", "Energy", hook="their new can")], backend)
    lead = report.leads[0]
    assert (lead.company, lead.email, lead.category, lead.hook, lead.domain, lead.target_name) == ("A", "jo@a.com", "Energy", "their new can", "a.com", "Jo Ng")
    assert lead.notes.startswith("[found] Stub: Partnerships Manager, confidence 90 (valid)")
    assert report.searched == 1 and report.searches == 1


def test_discover_skips_existing_domains_banned_brands_and_respects_the_limit():
    backend = StubBackend({n: found(f"jo@{n.lower()}.com") for n in "ABCDEF"})
    targets = [T("A"), T("B"), T("Celsius"), T("D"), T("E"), T("F")]
    report = D.discover(targets, backend, existing_domains={"b.com"}, banned_brands=["Celsius"], limit=2)
    assert backend.seen == ["A", "D"] and report.remaining == 2
    assert dict(report.skipped) == {"B": "b.com is already in the sheet", "Celsius": "existing partner (BANNED_BRANDS)"}


def test_discover_reports_brands_with_no_contacts_and_isolates_errors():
    backend = StubBackend({"A": DiscoveryError("A broke"), "B": D.LookupResult(searches=2), "C": found("jo@c.com")})
    report = D.discover([T("A"), T("B"), T("C")], backend)
    assert report.errors == [("A", "A broke")] and report.without_contacts == ["B"] and [l.company for l in report.leads] == ["C"]
    assert report.searches == 3 and report.searched == 3 and not report.aborted


def test_discover_keeps_what_it_found_when_the_quota_runs_out():
    backend = StubBackend({"A": found("jo@a.com"), "B": QuotaExceeded("plan used up")})
    report = D.discover([T("A"), T("B"), T("C"), T("D")], backend)
    assert [l.company for l in report.leads] == ["A"] and report.aborted == "plan used up"
    assert backend.seen == ["A", "B"] and report.remaining == 3  # B, C and D were not completed


def test_discover_records_partial_finds_then_stops_when_a_backend_says_stop():
    partial = D.LookupResult([D.Contact("jo@a.com", "Jo", "Ng", "Partnerships Manager", 90, "valid", "Stub")], "a.com", searches=2, stop="out of lookups")
    backend = StubBackend({"A": partial})
    report = D.discover([T("A"), T("B"), T("C")], backend)
    assert [l.email for l in report.leads] == ["jo@a.com"] and report.aborted == "out of lookups"
    assert backend.seen == ["A"] and report.remaining == 2  # B and C never ran; A is done

    empty = D.LookupResult(stop="out of lookups")
    backend = StubBackend({"A": empty})
    report = D.discover([T("A"), T("B")], backend)
    assert report.leads == [] and report.remaining == 2 and report.without_contacts == []  # A was cut off, not "empty"


def test_discover_stops_on_a_rejected_key():
    backend = StubBackend({"A": DiscoveryAuthError("bad key")})
    report = D.discover([T("A"), T("B")], backend)
    assert report.aborted == "bad key" and report.leads == [] and backend.seen == ["A"]


def test_discover_never_returns_the_same_address_twice():
    backend = StubBackend({"A": found("jo@shared.com"), "B": found("jo@shared.com", "al@b.com")})
    report = D.discover([T("A"), T("B")], backend)
    assert [(l.company, l.email) for l in report.leads] == [("A", "jo@shared.com"), ("B", "al@b.com")]
    backend = StubBackend({"A": found("jo@shared.com"), "B": found("jo@shared.com")})
    assert D.discover([T("A"), T("B")], backend).without_contacts == ["B"]


def test_plan_lookups_is_a_free_preview():
    todo, skipped, left = D.plan_lookups([T("A"), T("B"), T("Celsius"), T("C")], existing_domains={"a.com"}, banned_brands=["celsius"], limit=1)
    assert [t.company for t in todo] == ["B"] and left == 1 and len(skipped) == 2
    todo, _, left = D.plan_lookups([T("A"), T("B")], limit=None)
    assert len(todo) == 2 and left == 0


def test_discovered_leads_flow_into_the_sheet_without_duplicates():
    from src.sheet_manager import SheetManager
    ws = FakeWorksheet(grid=[list(COLUMNS)])
    sheet = SheetManager(FakeSpreadsheet(ws), ws, sleep=lambda s: None)
    leads = [Lead("A", "jo@a.com", "Energy", "Jo Ng", domain="a.com", notes="[found] Stub: x"), Lead("B", "al@b.com", "Gears")]
    assert len(sheet.append_leads(leads).added_ids) == 2
    again = sheet.append_leads(leads)
    assert again.added_ids == [] and len(again.duplicates) == 2
    rows = sheet.read_rows()
    assert rows[0].notes == "[found] Stub: x" and rows[0].approval == "Pending" and rows[1].category == "Gears"


# --------------------------------------------------------------------------- remembering dead ends
def test_the_lookup_log_survives_a_restart_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "data" / "lookups.json"
    log = D.LookupLog(path)
    assert not path.exists() and not log.damaged and log.entries == {}
    log.record("cerave.com", "CeraVe", date(2026, 10, 18))
    log.save()
    assert D.LookupLog(path).entries == {"cerave.com": {"company": "CeraVe", "date": "2026-10-18"}}
    assert sorted(p.name for p in path.parent.iterdir()) == ["lookups.json"]
    assert json.loads(path.read_text())["version"] == 1


def test_a_log_with_nothing_new_writes_nothing(tmp_path):
    path = tmp_path / "lookups.json"
    D.LookupLog(path).save()
    assert not path.exists()
    D.LookupLog(None).save()  # no path at all is fine too


def test_forgetting_a_brand_removes_it_from_the_file(tmp_path):
    path = tmp_path / "lookups.json"
    log = D.LookupLog(path)
    log.record("a.com", "A", date(2026, 10, 18))
    log.record("b.com", "B", date(2026, 10, 18))
    log.save()
    log.forget("a.com")
    log.forget("never-there.com")
    log.save()
    assert list(D.LookupLog(path).entries) == ["b.com"]


@pytest.mark.parametrize("days_later,remembered", [(0, True), (1, True), (29, True), (30, False), (31, False), (400, False)])
def test_a_dead_end_is_remembered_for_exactly_the_recheck_window(days_later, remembered):
    log = D.LookupLog(None)
    log.record("cerave.com", "CeraVe", date(2026, 10, 18))
    today = date.fromordinal(date(2026, 10, 18).toordinal() + days_later)
    reason = log.skip_reason("cerave.com", today, 30)
    assert bool(reason) is remembered
    if remembered:
        assert "searched on 2026-10-18" in reason and "searched again on 2026-11-17" in reason and "--recheck-days 0" in reason


def test_recheck_zero_a_new_brand_and_a_clock_that_moved_backwards_never_block_a_search():
    log = D.LookupLog(None)
    log.record("cerave.com", "CeraVe", date(2026, 10, 18))
    assert log.skip_reason("cerave.com", date(2026, 10, 18), 0) == ""
    assert log.skip_reason("other.com", date(2026, 10, 18), 30) == ""
    assert log.skip_reason("cerave.com", date(2026, 9, 1), 30) == ""  # the saved date is in the future: do not trust it


@pytest.mark.parametrize("content", [
    "{ not json", "[]", '{"dead_ends": []}', '{"dead_ends": {"a.com": {"date": "last tuesday"}}}',
    '{"dead_ends": {"a.com": {}}}', '{"other": 1}', "",
])
def test_a_damaged_log_starts_over_instead_of_failing(tmp_path, content):
    path = tmp_path / "lookups.json"
    path.write_text(content)
    log = D.LookupLog(path)
    assert log.damaged and log.entries == {}
    log.record("a.com", "A", date(2026, 10, 18))
    log.save()
    assert not log.damaged and D.LookupLog(path).entries["a.com"]["date"] == "2026-10-18"


def test_remembered_brands_are_skipped_before_the_limit_is_counted():
    remembered = {"b.com": "searched on 2026-10-10 with no usable contact"}
    todo, skipped, left = D.plan_lookups([T("A"), T("B"), T("C"), T("D")], remembered=remembered, limit=2)
    assert [t.company for t in todo] == ["A", "C"] and left == 1  # B did not use up one of the two slots
    assert skipped == [("B", "searched on 2026-10-10 with no usable contact")]


def test_discover_reports_which_brands_found_people_and_which_were_dead_ends():
    backend = StubBackend({"A": found("jo@a.com"), "B": D.LookupResult(searches=2), "C": DiscoveryError("C broke"),
                           "D": found("al@d.com")})
    report = D.discover([T("A"), T("B"), T("C"), T("D")], backend)
    assert report.found_keys == ["a.com", "d.com"] and report.dead_end_keys == ["b.com"]  # an error is not a dead end


def test_a_quota_stop_is_not_recorded_as_a_dead_end():
    backend = StubBackend({"A": D.LookupResult(searches=1, stop="plan used up")})
    report = D.discover([T("A"), T("B")], backend)
    assert report.dead_end_keys == [] and report.found_keys == [] and report.aborted == "plan used up"


# --------------------------------------------------------------------------- Hunter credit balance
def account_body(**requests_blocks):
    return {"data": {"first_name": "Varun", "plan_name": "Free", "plan_level": 0, "reset_date": "2026-11-19", "requests": requests_blocks}}


CREDITS = {"used": 0, "available": 50.0, "remaining": 50.0}


def test_hunter_balance_reads_the_free_account_endpoint_with_the_key_in_a_header():
    session = Session(Resp(200, account_body(credits=CREDITS, searches={"used": 0, "available": 50, "remaining": 50})))
    balance = D.HunterBackend("SECRET-KEY", session=session, **NO_SLEEP).balance()
    assert balance == D.Balance("Free", 50.0, 50.0, "2026-11-19")
    kind, url, kw = session.calls[0]
    assert (kind, url) == ("GET", "https://api.hunter.io/v2/account") and kw["headers"]["X-API-KEY"] == "SECRET-KEY"
    assert "SECRET-KEY" not in json.dumps(kw["params"])


def test_hunter_balance_prefers_the_credits_pool_and_falls_back_to_searches():
    both = Session(Resp(200, account_body(credits={"remaining": 12.5, "available": 50}, searches={"remaining": 99, "available": 99})))
    assert D.HunterBackend("k", session=both, **NO_SLEEP).balance().remaining == 12.5
    only_searches = Session(Resp(200, account_body(searches={"remaining": 7, "available": 25}, verifications={"remaining": 3, "available": 100})))
    assert D.HunterBackend("k", session=only_searches, **NO_SLEEP).balance() == D.Balance("Free", 7.0, 25.0, "2026-11-19")


def test_hunter_balance_copes_with_a_missing_available_figure_and_a_missing_plan():
    body = {"data": {"requests": {"credits": {"remaining": 3.5}}}}
    assert D.HunterBackend("k", session=Session(Resp(200, body)), **NO_SLEEP).balance() == D.Balance("", 3.5, 3.5, "")


@pytest.mark.parametrize("body", [
    {}, {"data": None}, {"data": []}, {"data": {}}, {"data": {"requests": []}}, {"data": {"requests": {}}},
    {"data": {"requests": {"credits": "lots"}}}, {"data": {"requests": {"credits": {"remaining": "lots"}}}},
    {"data": {"requests": {"credits": {"remaining": None}, "searches": {"remaining": float("nan")}}}},
    {"data": {"requests": {"credits": {"remaining": float("inf")}}}},
])
def test_hunter_balance_is_none_for_a_reply_in_an_unexpected_shape(body):
    assert D.HunterBackend("k", session=Session(Resp(200, body)), **NO_SLEEP).balance() is None


def test_hunter_balance_reports_a_rejected_key():
    with pytest.raises(DiscoveryAuthError, match="HUNTER_API_KEY"):
        D.HunterBackend("bad", session=Session(Resp(401, {"errors": [{"id": "authentication_failed"}]})), **NO_SLEEP).balance()


def test_number_parsing_ignores_junk():
    assert D._number("12.5") == 12.5 and D._number(3) == 3.0
    for junk in (None, "x", [], {}, float("nan"), float("inf")):
        assert D._number(junk) is None
