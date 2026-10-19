# Operation Sponsorships

Cold-outreach pipeline for **Penn State Men's Club Volleyball** (top-10 NCVF program, 30 athletes) to land brand
partnerships, product sponsorships, and sample seeding all year.

It is built around one rule: **a human approves every email.** Nothing is sent unless you approve it in a Google
Sheet, and by default the mailer only creates Gmail *drafts* so you press Send yourself.

```
discover leads -> LLM drafts -> you review in the Sheet -> Gmail drafts (default) / live send (opt-in)
```

> **This repository is public.** Never commit API keys, `credentials.json`, `token.json`, or lead contact lists.
> `.gitignore` and a pre-commit hook (below) block the common mistakes.

## Status

| Piece | State |
|---|---|
| Config, settings, logging, errors (`config/`) | done |
| Virtualenv + pinned dependencies | done |
| Secret-blocking pre-commit hook | done, tested |
| Google OAuth loader (`config/credentials.py`) | done, tested (one login covers Sheets, Drive and Gmail) |
| Sheet manager (`src/sheet_manager.py`) | done, tested (dropdowns, colors, Row ID lookup, edit-conflict guard, retries) |
| LLM layer (`src/llm.py`, Claude or Gemini), guardrails, drafter (`src/drafter.py`) | done, tested |
| Mailer: drafts and rate-limited send (`src/mailer.py`) | next |
| Lead discovery: Hunter / Apollo / CSV (`src/discovery.py`) | planned |
| CLI (`main.py`) and tests | planned |

This table is updated with every commit. 211 automated tests run offline with `.venv/bin/python -m pytest`.

## Setup (what works today)

System Python on macOS is 3.9, but every current dependency needs 3.10+. This project uses 3.12.

```bash
brew install python@3.12
cd operation_sponsorships
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt

# Turn on the secrets guard (once per clone)
git config core.hooksPath scripts/hooks
```

Copy `.env.template` to `.env` and fill it in as features land. `.env` is gitignored.

## Safety model

- **Approval gate.** Only rows with `Approval Status = Approved` and `Send Status = Unsent` are ever mailed.
- **Draft-first.** `send` creates Gmail drafts by default. Real sending needs `--live`.
- **Hard limits** (in `config/settings.py`): at most 15 emails per run and a random 180 to 420 second pause between
  live sends. `MAX_EMAILS_PER_RUN` can lower the cap but never raise it.
- **Sender lock.** The mailer refuses to run unless the signed-in Google account is `pennstatemcvb@gmail.com`.
- **Public-repo guard.** `scripts/hooks/pre-commit` blocks `.env`, OAuth files, logs, lead CSVs, and anything that
  looks like an API key or token.

## The review sheet

One Google Sheet is the queue. You edit drafts right in the cell, flip `Approval Status`, and the tools do the rest.

| Column | Who fills it |
|---|---|
| Row ID | tool (stable key; safe to sort and filter the sheet) |
| Company Name, Category, Target Name, Target Email, Custom Hook / Angle | import or you |
| Generated Subject, Generated Body | drafter, then you edit freely |
| Approval Status | drafter sets `Pending Approval`; **you** set `Approved` or `Rejected` |
| Send Status | mailer: `Unsent` -> `Sending` -> `Drafted` / `Sent` / `Failed` |
| Sent Timestamp, Gmail ID, Notes, Company Domain | tool |

`Approval Status` is `Pending` (needs a draft), `Pending Approval` (draft ready for you), `Approved`, or `Rejected`.

How the sheet protects you:
- Columns are found by header name, so you can reorder them.
- Every write re-finds the row by Row ID first, so sorting mid-run cannot hit the wrong lead.
- If you change a row while a run is in flight, the tool detects it and skips that row.
- Only the cells a step owns are written; your edits elsewhere are never overwritten.
- Everything is written as plain text, so no generated text can become a spreadsheet formula.

## Google sign-in

One browser login (`python main.py auth`, coming with the CLI) covers Sheets, Drive and Gmail.
The login is saved to `config/token.json` (gitignored, owner-only permissions) and refreshed automatically.
It refuses to save unless you signed in as pennstatemcvb@gmail.com and granted every permission.
If the Google Cloud consent screen is left in "Testing", Google expires the login every 7 days; the README setup section
will cover publishing the app to avoid that.

## How drafting works

`python main.py draft` (CLI coming) takes every row that is `Pending` + `Unsent` and has a valid email.

1. The model gets the company, its category, and your optional `Custom Hook / Angle`. It never sees the contact's name or email.
2. The model writes only the subject and the middle of the email. Code adds the greeting, the opt-out line, the sign-off
   and the signature (name, title, email, both Instagram handles), so those can never drift or be misspelled.
3. Deterministic checks run on the result. A draft must: contain the required "partnered with brands in the past"
   sentence word for word, say top 10 / NCVF / 30 athletes, mention the campus groups and the season, make a clear ask,
   stay at 90 to 170 words, and avoid banned brands, dashes, semicolons, exclamation marks, emoji, markdown,
   placeholders, stock phrases, and any number except 10 and 30.
4. Easy problems are repaired automatically (long dashes become commas, a stray greeting or sign-off is trimmed).
   Anything else goes back to the model once with the exact list of problems.
5. A draft that still fails is **not** written. The row stays `Pending` and the reason goes in `Notes`.
6. A good draft is written with `Approval Status = Pending Approval`. Nothing is sent until you set `Approved`.

Rows are skipped, with a note, when there is no valid email, the company is on `BANNED_BRANDS`, or the row already has
draft text (clear the subject and body cells to have it rewritten). If you edit a row while a draft is being written,
your edit wins and the draft is dropped.

Pick the model with `LLM_PROVIDER` in `.env`:

| Provider | Default model | Notes |
|---|---|---|
| `anthropic` | `claude-opus-5-5` | Claude. `ANTHROPIC_MODEL=claude-sonnet-5-5` is cheaper. Refusal fallback is on by default. |
| `gemini` | `gemini-3.8-flash` | Google Gemini. Confirm the name with `python main.py doctor`. |

Season wording follows the calendar. August to September says we are in the middle of the fall slate, October to
December says we are wrapping it up and gearing up for spring, January to April says we are in the spring run with
regionals and nationals ahead, and May to July says we are planning for next season.

## Pitch rules the drafter must follow

- Never name brands we already work with. The line is exactly: *"We have partnered with brands in the past and are
  always excited to showcase and actively promote products our athletes love."*
- Lead with the 30-athlete roster, the top-10 national NCVF standing, and reach across fraternities, cultural
  groups, the honors college, and other club sports.
- Season framing follows the calendar. Today (October) that means wrapping up the fall slate and gearing up for the
  full spring run.
- Direct, professional, human. No filler, no em dashes.

## Layout

```
config/    settings, error types, logging, Google OAuth loader, Google error messages
src/       models, sheet_manager, llm, guardrails, drafter (done); mailer, discovery (coming)
scripts/   hooks/pre-commit
data/      targets.example.csv (your real lead CSVs stay local and gitignored)
tests/     pytest, no network
```
