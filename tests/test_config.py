import logging
import stat
from datetime import date

import pytest

from config.errors import ConfigError
from config.logging_setup import NOISY_LOGGERS, quiet_noisy_loggers
from config.settings import (
    HARD_CAP_PER_RUN, MAX_DELAY_SECONDS, MIN_DELAY_SECONDS, REQUIRED_PARTNER_LINE, Sender, Settings,
    mask, parse_spreadsheet_id, season_context, update_env_file,
)


def test_defaults_match_the_brief():
    s = Settings.load(env={})
    assert s.sender.name == "Varun Kaveti"
    assert s.sender.email == "pennstatemcvb@gmail.com"
    assert s.sender.team_instagram == "@psumensclubvolleyball"
    assert s.sender.personal_instagram == "@varunkaveti"
    assert s.llm_provider == "anthropic" and s.anthropic_model == "claude-opus-5-5"
    assert s.banned_brands == ("Celsius",)
    assert (MIN_DELAY_SECONDS, MAX_DELAY_SECONDS, HARD_CAP_PER_RUN) == (180, 420, 15)


def test_signature_ends_with_both_instagrams():
    lines = Sender().signature().splitlines()
    assert lines[0] == "Varun Kaveti"
    assert lines[-2] == "Team Instagram: @psumensclubvolleyball"
    assert lines[-1] == "Personal Instagram: @varunkaveti"


def test_cap_can_be_lowered_never_raised():
    assert Settings.load(env={"MAX_EMAILS_PER_RUN": "5"}).max_per_run == 5
    assert Settings.load(env={"MAX_EMAILS_PER_RUN": "500"}).max_per_run == 15
    assert Settings.load(env={"MAX_EMAILS_PER_RUN": "0"}).max_per_run == 1
    with pytest.raises(ConfigError):
        Settings.load(env={"MAX_EMAILS_PER_RUN": "lots"})


def test_invalid_choices_are_rejected():
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        Settings.load(env={"LLM_PROVIDER": "openai"})
    with pytest.raises(ConfigError, match="DISCOVERY_BACKEND"):
        Settings.load(env={"DISCOVERY_BACKEND": "zoominfo"})


def test_blank_values_fall_back_to_defaults():
    s = Settings.load(env={"ANTHROPIC_MODEL": "  ", "WORKSHEET_NAME": ""})
    assert s.anthropic_model == "claude-opus-5-5" and s.worksheet_name == "Outreach Queue"


def test_requirement_errors_say_how_to_fix():
    s = Settings.load(env={})
    with pytest.raises(ConfigError, match="configure"):
        s.require_llm_key()
    with pytest.raises(ConfigError, match="init-sheet"):
        s.require_sheet()
    with pytest.raises(ConfigError, match="import --csv"):
        s.discovery_key()
    g = Settings.load(env={"LLM_PROVIDER": "gemini"})
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        g.require_llm_key()
    ok = Settings.load(env={"ANTHROPIC_API_KEY": "k", "SPREADSHEET_ID": "abc", "HUNTER_API_KEY": "h"})
    assert (ok.require_llm_key(), ok.require_sheet(), ok.discovery_key()) == ("k", "abc", "h")


def test_secrets_never_appear_in_repr():
    s = Settings.load(env={"ANTHROPIC_API_KEY": "sk-ant-secret-value", "HUNTER_API_KEY": "hunter-secret"})
    assert "secret" not in repr(s)


def test_spreadsheet_id_accepts_a_pasted_url():
    url = "https://docs.google.com/spreadsheets/d/1AbC-dEf_123/edit#gid=0"
    assert parse_spreadsheet_id(url) == "1AbC-dEf_123"
    assert parse_spreadsheet_id("  1AbC-dEf_123 ") == "1AbC-dEf_123"
    assert Settings.load(env={"SPREADSHEET_ID": url}).spreadsheet_id == "1AbC-dEf_123"


@pytest.mark.parametrize("day,key", [
    (date(2026, 10, 18), "late_fall"), (date(2026, 9, 3), "early_fall"), (date(2027, 2, 10), "spring"),
    (date(2027, 6, 1), "offseason"), (date(2026, 12, 31), "late_fall"), (date(2027, 1, 1), "spring"),
])
def test_season_follows_the_calendar(day, key):
    assert season_context(day).key == key


def test_october_uses_the_requested_framing():
    phrase = season_context(date(2026, 10, 18)).phrase
    assert "wrapping up our fall tournament slate" in phrase and "full spring competitive run" in phrase


def test_required_line_is_exact():
    assert REQUIRED_PARTNER_LINE == (
        "We have partnered with brands in the past and are always excited to showcase "
        "and actively promote products our athletes love."
    )


def test_update_env_file_keeps_comments_and_locks_permissions(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keep me\nLLM_PROVIDER=anthropic\nANTHROPIC_API_KEY=\n\nOTHER=1\n")
    update_env_file({"ANTHROPIC_API_KEY": "sk-ant-abc", "NEW_ONE": "has space"}, path=env)
    text = env.read_text()
    assert "# keep me" in text and "OTHER=1" in text and "LLM_PROVIDER=anthropic" in text
    assert "ANTHROPIC_API_KEY=sk-ant-abc" in text and 'NEW_ONE="has space"' in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    again = Settings.load(env_file=None, env={"ANTHROPIC_API_KEY": "sk-ant-abc"})
    assert again.anthropic_api_key == "sk-ant-abc"


def test_mask_hides_most_of_a_secret():
    assert mask("") == "(not set)"
    assert mask("sk-ant-api03-abcdefghijkl") == "sk-a...jkl"
    assert mask("short") == "set"


def test_chatty_library_loggers_are_hidden_unless_verbose():
    saved = {name: logging.getLogger(name).level for name in NOISY_LOGGERS}
    try:
        quiet_noisy_loggers(False)
        assert "google_genai" in NOISY_LOGGERS
        for name in NOISY_LOGGERS:
            assert not logging.getLogger(name).isEnabledFor(logging.INFO), name
        sdk = logging.getLogger("google_genai.models")  # the logger behind "AFC is enabled with max remote calls"
        assert not sdk.isEnabledFor(logging.INFO) and sdk.isEnabledFor(logging.WARNING)
        quiet_noisy_loggers(True)
        assert sdk.isEnabledFor(logging.DEBUG)
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
