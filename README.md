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
| Google OAuth loader (`config/credentials.py`) | next |
| Sheet manager (`src/sheet_manager.py`) | planned |
| LLM drafter + guardrails (`src/drafter.py`) | planned |
| Mailer: drafts and rate-limited send (`src/mailer.py`) | planned |
| Lead discovery: Hunter / Apollo / CSV (`src/discovery.py`) | planned |
| CLI (`main.py`) and tests | planned |

This table is updated with every commit.

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
config/    settings, error types, logging (credentials loader coming)
src/       discovery, sheet_manager, drafter, mailer (coming)
scripts/   hooks/pre-commit
data/      targets.example.csv (your real lead CSVs stay local and gitignored)
tests/     pytest, no network
```
