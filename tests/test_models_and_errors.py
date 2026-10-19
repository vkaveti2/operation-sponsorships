import pytest

from config.google_errors import (
    describe_exception, explain_google_error, is_retryable, parse_google_error, summarize_exception,
)
from src.models import (
    Lead, LeadRow, is_generic_mailbox, normalize_category, normalize_domain, normalize_email,
)
from config.settings import Col
from tests.fakes import api_error


def test_email_cleanup():
    assert normalize_email(" <Jane.Doe@Brand.COM> ") == "jane.doe@brand.com"
    assert normalize_email("mailto:a@b.co") == "a@b.co"
    for bad in ("", None, "not an email", "a@b", "a b@c.com", "@c.com"):
        assert normalize_email(bad) == ""


def test_generic_mailboxes_but_partner_inboxes_survive():
    assert is_generic_mailbox("info@x.com") and is_generic_mailbox("Support@x.com")
    assert not is_generic_mailbox("partnerships@x.com")
    assert not is_generic_mailbox("sponsorships@x.com")
    assert not is_generic_mailbox("jane@x.com")


def test_category_aliases():
    assert normalize_category("Skin Care") == "Skincare"
    assert normalize_category("tape") == "Recovery"
    assert normalize_category("ELECTROLYTES") == "Hydration"
    assert normalize_category("energy drink") == "Energy"
    assert normalize_category("Gear") == "Gear"
    assert normalize_category("pet food") == "" and normalize_category(None) == ""


def test_domain_cleanup():
    assert normalize_domain("https://www.RedBull.com/en-us") == "redbull.com"
    assert normalize_domain("liquid-iv.com") == "liquid-iv.com"
    assert normalize_domain("jane@cerave.com") == "cerave.com"
    assert normalize_domain("nope") == "" and normalize_domain("") == "" and normalize_domain(None) == ""


def test_lead_cell_values_and_row_accessors():
    lead = Lead(company=" Red Bull ", email="a@b.com", category="Energy", target_name="Jo", domain="redbull.com")
    cells = lead.cell_values()
    assert cells[Col.COMPANY] == "Red Bull" and cells[Col.DOMAIN] == "redbull.com"
    row = LeadRow({Col.ROW_ID: " L1 ", Col.COMPANY: "Red Bull", Col.TARGET_EMAIL: "a@b.com"}, sheet_row=4)
    assert row.row_id == "L1" and row.email == "a@b.com" and row.subject == "" and row.sheet_row == 4
    assert "Red Bull" in row.label() and "L1" in row.label()


def test_parse_google_error_shapes():
    body = {"error": {"code": 403, "message": "nope", "errors": [{"reason": "rateLimitExceeded"}]}}
    assert parse_google_error(body) == (403, "rateLimitExceeded", "nope")
    assert parse_google_error('{"error": {"code": 404, "message": "gone", "status": "NOT_FOUND"}}') == (404, "NOT_FOUND", "gone")
    assert parse_google_error({"error": "invalid_grant", "error_description": "Token expired"}) == (0, "invalid_grant", "Token expired")
    assert parse_google_error("<html>bad gateway</html>")[0] == 0
    assert parse_google_error(None)[0] == 0


def test_describe_exception_handles_gspread_and_googleapiclient():
    exc = api_error(429, "Quota exceeded", "RESOURCE_EXHAUSTED")
    assert describe_exception(exc)[:2] == (429, "RESOURCE_EXHAUSTED")

    class FakeHttpError(Exception):
        content = b'{"error": {"code": 403, "message": "Gmail API has not been used in project 1 before", "status": "PERMISSION_DENIED"}}'
        resp = type("R", (), {"status": 403})()

    status, reason, message = describe_exception(FakeHttpError())
    assert status == 403 and "has not been used" in message
    assert "not enabled" in summarize_exception(FakeHttpError(), "gmail")
    assert "console.cloud.google.com/apis/library/gmail.googleapis.com" in summarize_exception(FakeHttpError(), "gmail")


def test_explanations_are_actionable():
    assert "python main.py auth" in explain_google_error("sheets", 401, "", "")
    assert "python main.py auth" in explain_google_error("gmail", 403, "insufficientPermissions", "")
    assert "rate limiting" in explain_google_error("sheets", 429, "", "")
    assert "could not find" in explain_google_error("sheets", 404, "", "")
    assert "server problem" in explain_google_error("drive", 503, "", "")


def test_retryable_rules():
    assert is_retryable(429) and is_retryable(503) and is_retryable(408)
    assert is_retryable(403, "rateLimitExceeded")
    assert not is_retryable(403, "forbidden") and not is_retryable(404) and not is_retryable(400)
