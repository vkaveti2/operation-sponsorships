"""Deterministic checks on email text. No model is trusted to follow the rules on its own.

Two levels:
  * check_draft     strict. Run on what the model wrote, before it ever reaches the sheet.
  * lint_for_send   light. Run on the sheet text right before Gmail, after a human may have edited it.

Special characters are written as code points, for example chr(0x2014), so this file never
contains an invisible or look-alike character.
"""
from __future__ import annotations

import re
from typing import Iterable

from config.settings import REQUIRED_PARTNER_LINE
from src.models import normalize_email

SUBJECT_MAX = 70
BODY_MIN_WORDS = 90  # counted on the model's paragraphs only (no greeting, signature, opt-out)
BODY_MAX_WORDS = 170
ALLOWED_ACRONYMS = frozenset({"NCVF", "NCAA", "USAV"})  # all-caps words that are not shouting
ALLOWED_NUMBERS = frozenset({"10", "30"})  # the only facts stated as digits; any other number is written as a word

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
ELLIPSIS = chr(0x2026)
BULLET = chr(0x2022)

# Look-alike characters models like to produce, mapped to plain ASCII.
_CHAR_FIXES = {
    0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"',  # curly quotes
    0x00A0: " ", 0x2009: " ", 0x202F: " ",  # odd spaces
    0x200B: "", 0xFEFF: "",  # zero-width characters
    0x2026: "...",  # ellipsis
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2212: "-",  # hyphen look-alikes and the minus sign
}

FLUFF_PHRASES = (
    "hope this email finds you well", "hope this finds you well", "hope you are doing well",
    "hope you're doing well", "hope you are well", "i wanted to reach out", "i am reaching out",
    "i'm reaching out", "i am writing to", "i'm writing to", "touch base", "circle back",
    "synergy", "game-changer", "game changer", "leverage", "unlock", "revolutionary",
    "cutting-edge", "cutting edge", "world-class", "state-of-the-art", "thrilled", "delighted",
    "pleasure to meet", "i came across", "to whom it may concern", "dear sir", "valued partner",
    "win-win", "next level", "fast-paced", "rest assured", "don't hesitate", "do not hesitate",
    "at your earliest convenience", "per my last", "moving forward", "reach new heights",
    "i am excited to", "i'm excited to", "i would love to", "i'd love to",
    "unsubscribe", "opt out", "opt-out",
)

_EMOJI_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF))
_EMOJI = re.compile(
    "[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _EMOJI_RANGES) + chr(0xFE0F) + chr(0x200D) + "]"
)
_TOP10 = re.compile(r"\btop[\s-]?(10|ten)\b", re.I)
_NCVF = re.compile(r"\bNCVF\b|National Collegiate Volleyball Federation", re.I)
_THIRTY = re.compile(r"\b(30|thirty)\b", re.I)
_REACH_GROUPS = {
    "fraternities": re.compile(r"fraternit|greek", re.I),
    "cultural groups": re.compile(r"cultural", re.I),
    "the honors college": re.compile(r"honors", re.I),
    "other club sports": re.compile(r"club (sport|team|athletic)|other club", re.I),
}
_SEASON_RULES = {
    "early_fall": (r"\bfall\b", r"\bspring\b", "fall and spring"),
    "late_fall": (r"\bfall\b", r"\bspring\b", "fall and spring"),
    "spring": (r"\bspring\b", r"regional|national", "the spring season and regional or national tournaments"),
    "offseason": (r"next season|upcoming season|\bfall\b", None, "the coming season"),
}
_MARKDOWN_LINE = re.compile(rf"^\s*(#{{1,6}}\s|[-*{BULLET}]\s|\d+[.)]\s|>\s)", re.M)
_PLACEHOLDER = re.compile(
    r"\[[^\]\n]{1,40}\]|\{[^}\n]{1,40}\}|<[^>\n]{1,40}>|%[A-Z_]{3,}%"
    r"|\bTBD\b|\bXXX+\b|lorem ipsum|(?-i:\bINSERT\b)|(?-i:\bFIRST NAME\b)",
    re.I,
)
_ALLCAPS_WORD = re.compile(r"\b[A-Z]{4,}\b")  # 3 letters stays free for PSU, USA, SPF
_NUMBER = re.compile(r"\d+(?:[,.]\d+)*")
_GREETING_LINE = re.compile(r"^\s*(hi|hello|hey|dear)\b[^\n]{0,60}[,:]\s*$", re.I)
_SIGNOFF_LINE = re.compile(
    r"^\s*(best|best regards|kind regards|warm regards|regards|thanks|thank you|many thanks|sincerely|"
    r"cheers|respectfully|talk soon|all the best)[,.!]?\s*$",
    re.I,
)


# --------------------------------------------------------------------------- text cleanup
def tidy_text(text: str) -> str:
    """Normalise punctuation the way a careful human editor would.

    Curly quotes and look-alike hyphens become plain ASCII, em and en dashes become commas, and
    stray spaces and blank lines are removed. Paragraph breaks are kept. Anything that needs
    judgement (semicolons, stock phrases) is left for check_draft to report.
    """
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = s.translate(_CHAR_FIXES)
    s = re.sub(rf"(\d)[ \t]*{EN_DASH}[ \t]*(\d)", r"\1-\2", s)  # numeric ranges keep a plain hyphen
    s = re.sub(rf"[ \t]*[{EM_DASH}{EN_DASH}][ \t]*", ", ", s)  # em and en dashes become commas
    s = re.sub(r"[ \t]+-{1,2}[ \t]+", ", ", s)  # " - " and " -- " used as dashes
    s = re.sub(r",[ \t]*,", ",", s)
    s = re.sub(r"[ \t]+,", ",", s)
    s = re.sub(r",[ \t]*([.!?])", r"\1", s)
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def strip_wrapper(text: str) -> str:
    """Remove a standalone greeting line at the top and a sign-off block at the bottom.

    The greeting, sign-off, signature and opt-out are added by code, so a model that writes its
    own would create duplicates. Only whole lines are removed; an inline greeting is left in place
    for check_draft to report.
    """
    lines = tidy_text(text).split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and _GREETING_LINE.match(lines[0]):
        lines.pop(0)
    for index in range(len(lines) - 1, max(len(lines) - 8, -1), -1):
        if _SIGNOFF_LINE.match(lines[index]):
            lines = lines[:index]
            break
    return tidy_text("\n".join(lines))


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", tidy_text(text)).strip()


def word_count(text: str) -> int:
    return len(text.split())


def find_banned(text: str, brands: Iterable[str]) -> list[str]:
    """Banned brand names that appear as whole words, case-insensitively."""
    hits: list[str] = []
    for brand in brands:
        brand = brand.strip()
        if brand and re.search(rf"(?<!\w){re.escape(brand)}(?!\w)", text, re.I):
            hits.append(brand)
    return hits


def placeholder_tokens(text: str) -> list[str]:
    """Leftover template text such as [First Name] or {company}. Links and addresses in angle
    brackets are not placeholders."""
    tokens: list[str] = []
    for match in _PLACEHOLDER.finditer(text):
        token = match.group(0)
        if token.startswith("<") and ("@" in token or "://" in token or "." in token):
            continue
        tokens.append(token)
    return tokens


# --------------------------------------------------------------------------- strict draft check
def check_draft(
    subject: str,
    text: str,
    *,
    banned_brands: Iterable[str],
    season_key: str,
    context: Iterable[str] = (),
) -> list[str]:
    """Rule violations in a model-written draft. An empty list means it may go to the sheet.

    `text` is the middle of the email: no greeting, sign-off, signature, or opt-out line.
    `context` are the strings the model was given (company, contact, custom angle). ALL CAPS words and
    numbers that appear in them are legitimate, so "5-Hour Energy" or "SPF 50" are not flagged.
    Messages are written to be fed straight back to the model for a rewrite.
    """
    problems: list[str] = []
    subject = (subject or "").strip()
    body = (text or "").strip()
    given = [entry or "" for entry in context]
    caps_ok = ALLOWED_ACRONYMS | {w for entry in given for w in _ALLCAPS_WORD.findall(entry)}
    numbers_ok = ALLOWED_NUMBERS | {n for entry in given for n in _NUMBER.findall(entry)}
    flat = _squash(body)
    both = f"{subject}\n{body}"
    low = flat.lower()

    # --- subject
    if not subject:
        problems.append("The subject line is empty.")
    else:
        if len(subject) > SUBJECT_MAX:
            problems.append(f"The subject is {len(subject)} characters. Keep it under {SUBJECT_MAX}.")
        if re.match(r"^(re|fwd?)\s*:", subject, re.I):
            problems.append("The subject must not start with Re: or Fwd:.")
        if "!" in subject:
            problems.append("The subject must not contain an exclamation mark.")
        if any(w not in caps_ok for w in _ALLCAPS_WORD.findall(subject)):
            problems.append("The subject must not use ALL CAPS words.")

    # --- required facts
    if _squash(REQUIRED_PARTNER_LINE).lower() not in low:
        problems.append(
            f'The required sentence is missing or changed. Include it exactly as written: "{REQUIRED_PARTNER_LINE}"'
        )
    if not _TOP10.search(flat):
        problems.append('Say that the team is a top 10 nationally ranked program (use the words "top 10").')
    if not _NCVF.search(flat):
        problems.append("Name the National Collegiate Volleyball Federation (NCVF).")
    if not _THIRTY.search(flat):
        problems.append("State the roster size: 30 athletes.")
    missing_reach = [name for name, rx in _REACH_GROUPS.items() if not rx.search(flat)]
    if len(missing_reach) > 1:
        problems.append(
            "Mention the campus reach: fraternities, cultural groups, the honors college, and other club sports. "
            "Missing: " + ", ".join(missing_reach) + "."
        )
    first, second, label = _SEASON_RULES.get(season_key, _SEASON_RULES["late_fall"])
    if not re.search(first, flat, re.I) or (second and not re.search(second, flat, re.I)):
        problems.append(f"Frame the season as instructed (mention {label}).")
    if not re.search(r"sponsor|sample|seed|partner|collaborat", flat, re.I):
        problems.append("The closing must ask about or mention sponsorship, samples, partnership, or collaboration.")

    # --- length
    words = word_count(body)
    if words < BODY_MIN_WORDS:
        problems.append(f"The body is {words} words. Write {BODY_MIN_WORDS} to {BODY_MAX_WORDS}.")
    elif words > BODY_MAX_WORDS:
        problems.append(f"The body is {words} words. Cut it to {BODY_MAX_WORDS} or fewer.")

    # --- names we must never mention
    banned = find_banned(both, banned_brands)
    if banned:
        problems.append(
            "Remove these brand names completely: " + ", ".join(banned) + ". Use the required sentence instead."
        )

    # --- style
    if EM_DASH in both or EN_DASH in both or re.search(r"\s-{1,2}\s", both):
        problems.append("Do not use dashes as punctuation. Use commas or separate sentences.")
    if ";" in both:
        problems.append("Do not use semicolons. Write two sentences.")
    if "!" in body:
        problems.append("Do not use exclamation marks.")
    if "..." in both or ELLIPSIS in both:
        problems.append("Do not use ellipses.")
    if _EMOJI.search(both):
        problems.append("Do not use emoji or symbols.")
    if "**" in both or "__" in both or "`" in both or _MARKDOWN_LINE.search(body):
        problems.append("Use plain text only: no markdown, headings, or bullet points.")
    placeholders = placeholder_tokens(both)
    if placeholders:
        problems.append("Remove placeholder text such as " + ", ".join(placeholders[:3]) + ".")
    fluff = [p for p in FLUFF_PHRASES if p in low]
    if fluff:
        problems.append("Cut these stock phrases: " + ", ".join(f'"{p}"' for p in fluff[:5]) + ".")
    if re.match(r"^\s*(hi|hello|hey|dear)\b", body, re.I):
        problems.append("Do not write a greeting. It is added automatically.")
    if any(w not in caps_ok for w in _ALLCAPS_WORD.findall(body)):
        problems.append("Do not use ALL CAPS words.")
    invented = sorted({n for n in _NUMBER.findall(both) if n not in numbers_ok})
    if invented:
        problems.append(
            "Remove these numbers, they are not facts we gave you: " + ", ".join(invented[:4])
            + ". Only 10 and 30 may appear as digits; write anything else in words."
        )
    return problems


# --------------------------------------------------------------------------- light pre-send check
def lint_for_send(subject: str, body: str, to_email: str, banned_brands: Iterable[str]) -> list[str]:
    """Problems that must stop an email from leaving.

    Runs on text a human may have edited, so it only catches mistakes, not style: blank fields,
    leftover placeholders, banned brands, a bad address.
    """
    problems: list[str] = []
    if not normalize_email(to_email):
        problems.append(f"'{(to_email or '').strip()}' is not a valid email address")
    subject = subject or ""
    body = body or ""
    if not subject.strip():
        problems.append("the subject is empty")
    elif "\n" in subject or "\r" in subject:
        problems.append("the subject contains a line break")
    elif len(subject.strip()) > 200:
        problems.append("the subject is longer than 200 characters")
    if not body.strip():
        problems.append("the body is empty")
    elif word_count(body) < 20:
        problems.append("the body is under 20 words, which looks unfinished")
    tokens = placeholder_tokens(f"{subject}\n{body}")
    if tokens:
        problems.append("leftover placeholder text: " + ", ".join(tokens[:3]))
    banned = find_banned(f"{subject}\n{body}", banned_brands)
    if banned:
        problems.append("names a brand we agreed not to mention: " + ", ".join(banned))
    return problems
