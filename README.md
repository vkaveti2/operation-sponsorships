# Operation Sponsorships

Cold-outreach pipeline for **Penn State Men's Club Volleyball** (top 10 in the NCVF, 30 athletes) to land brand
partnerships, product sponsorships, and sample seeding all year.

It is built around one rule: **a human approves every email.** Nothing goes out unless you approve it in a Google
Sheet, and by default the mailer only creates Gmail *drafts* so you press Send yourself.

```
find leads  ->  model drafts  ->  you review and approve in the Sheet  ->  Gmail drafts (default)  or  live send
 discover        draft              (you, in your browser)                    send                      send --live
```

> **This repository is public.** Never commit API keys, `credentials.json`, `token.json`, or lead lists.
> `.gitignore` and a pre-commit hook block the common mistakes (see [Safety](#safety)).

## Contents
[Quick start](#quick-start) · [Setup](#setup-step-by-step) · [Daily cheat sheet](#daily-cheat-sheet) · [Try it safely](#try-it-safely-first) · [Daily use](#daily-use) · [The sheet](#the-review-sheet) ·
[Commands](#commands) · [Safety](#safety) · [How drafting works](#how-drafting-works) · [How sending works](#how-sending-works) ·
[Finding leads](#finding-leads) · [Troubleshooting](#troubleshooting) · [Development](#development)

## Quick start

```bash
brew install python@3.12
cd operation_sponsorships
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
git config core.hooksPath scripts/hooks          # turn on the secrets guard

source .venv/bin/activate                         # from here on, plain `python` is the project's Python
python main.py doctor                             # shows exactly what is still missing
python main.py configure                          # step 1: model API key (Claude or Gemini)
python main.py try-draft                          # see one real sample email (no Google login needed)
# step 2: Google Cloud setup (below), then:
python main.py auth                               # sign in as pennstatemcvb@gmail.com
python main.py init-sheet                         # create the review sheet
cp data/targets.example.csv data/targets.csv      # YOUR brand list: edit it (it is gitignored)
python main.py discover --dry-run                 # preview which brands would be looked up (free)
python main.py discover --limit 10                # find contacts at 10 of them (uses Hunter credits)
#   with contacts you already have instead:  python main.py import --csv data/my_contacts.csv
python main.py draft                              # the model writes emails
#   -> open the sheet, edit anything, set Approval Status to Approved
python main.py send                               # creates Gmail drafts. You press Send in Gmail
python main.py sync                               # marks the ones you sent
```

`python main.py doctor --online` tests every key and login without sending anything.

## Daily cheat sheet

**Nothing in this project runs by itself.** Every step is a command you type, and you are the approval step. Open a
terminal once per session:

```bash
cd ~/Documents/code/operation_sponsorships && source .venv/bin/activate
```

| How often | Command | What it does | What it uses |
|---|---|---|---|
| any time | `python main.py status` | counts by stage, what is waiting on you, the next command to run | nothing |
| weekly | `python main.py discover --limit 10` | finds contacts at 10 brands from `data/targets.csv` and adds them as `Pending` rows | Hunter credits (usually 1 per brand, at most 2) |
| each work day | `python main.py draft --limit 10` | writes emails for `Pending` rows | model tokens |
| each work day | *(in the Google Sheet)* | read, edit, set `Approval Status` to `Approved` or `Rejected` | nothing |
| each work day | `python main.py send --preview` | shows exactly what would be created and changes nothing | nothing |
| each work day | `python main.py send` | creates a Gmail draft for each `Approved` row. Open Gmail Drafts and press Send | nothing |
| each work day | `python main.py sync` | marks rows `Sent` once you have sent their drafts | nothing |

Now and then:

| Command | When |
|---|---|
| `python main.py doctor --online` | to check every key and login still works (also shows your Hunter credits) |
| `python main.py import --csv PATH` | you already have contacts with email addresses |
| `python main.py try-draft --company "Nuun" --category Hydration` | to see how the model writes for a particular brand |
| `python main.py send --live` | only when you decide to let it send for real (asks you to type `SEND`) |

A sensible rhythm: find leads once a week, then draft, review and send a handful each work day. A brand-new Gmail sender
does better with a small steady volume than with a burst, so treat 15 per run as a ceiling.

## Try it safely first

Each step is safe on its own and none of them sends an email. Stop at any step you are not happy with.

1. `python main.py doctor --online` checks the model key, the Google login, the club Gmail account and the sheet.
2. `python main.py try-draft` writes one real sample email. Nothing is saved or sent and no Google login is needed.
   Add `--show-prompt` to see exactly what the model is sent.
3. `python main.py discover --dry-run` lists the brands it would look up and your Hunter balance. It spends nothing.
4. `python main.py discover --limit 2 --no-broad` is the first real lookup: at most 2 Hunter credits. Open the sheet
   and look at the rows it added.
5. `python main.py draft --limit 2` writes their emails into the sheet. Read them there.
6. A test row that only goes to you: add a row by hand in the sheet with a made-up company, a category, and **your own
   email address** as `Target Email` (an alias like `you+test1@gmail.com` works and lets you repeat the test). Run
   `draft`, set that row to `Approved`, run `send`, open Gmail Drafts as the club account, press Send, and check it arrives.
   Then `sync`.

Only after step 6 look at real brands' emails and approve them.

## Setup, step by step

### 1. Python environment
macOS ships Python 3.9, but every current dependency needs 3.10+. This project uses 3.12 in a virtual environment
(`.venv`). If you ever see *"This project needs Python 3.10 or newer"*, you ran it with the wrong Python: use
`source .venv/bin/activate` first, or call `.venv/bin/python main.py ...` directly.

### 2. A model API key (pick one)

| Provider | Get a key | Default model |
|---|---|---|
| **Claude** (Anthropic) | <https://platform.claude.com/settings/keys> (add a few dollars of credit under Billing) | `claude-opus-5-5` |
| **Gemini** (Google) | <https://aistudio.google.com/apikey> | `gemini-3.8-flash` |

Run `python main.py configure`. The key is typed hidden and saved to `.env` (gitignored, owner-only permissions), then
tested. To pipe it in instead: `pbpaste | python main.py configure --key-from-stdin`.

Then `python main.py try-draft` writes one real sample email with that key (nothing saved, nothing sent, no Google login
needed). Gemini keys start with `AIza` or `AQ.`.

**Privacy.** The model is sent only the company name, its category, and your optional note, never a contact's name or
email address (`python main.py try-draft --show-prompt` prints the exact text). On Gemini's free tier Google says it may use
prompts to improve its products, and does not for a paid-tier key. Claude is not used that way by default.

Cost: Gemini's free tier costs nothing. On Claude expect a few cents per email with the default Opus model, so well under a
dollar for a batch of 15; `ANTHROPIC_MODEL=claude-sonnet-5-5` in `.env` is cheaper. `draft` prints the real token counts.

### 3. Google Cloud: one login for Sheets, Drive and Gmail

Everything below is done once, in a browser, **signed in as pennstatemcvb@gmail.com**. It takes about ten minutes.
Google renames console screens now and then. If a label differs slightly, search the console for the name in quotes.

**3a. Create a project.**
1. Go to <https://console.cloud.google.com/> and accept the terms if asked.
2. Click the project picker at the top, then **New Project**. Name it `operation-sponsorships`. Click **Create**, then
   make sure it is selected.

**3b. Turn on three APIs.** Open each link, check the project name at the top, and click **Enable**:
- Gmail API: <https://console.cloud.google.com/apis/library/gmail.googleapis.com>
- Google Sheets API: <https://console.cloud.google.com/apis/library/sheets.googleapis.com>
- Google Drive API: <https://console.cloud.google.com/apis/library/drive.googleapis.com>

(Drive is needed only to create the sheet and share it.)

**3c. Set up the consent screen** ("Google Auth Platform" in the menu).
1. Menu, **Google Auth Platform**, **Branding**. If it says *not configured yet*, click **Get Started**.
2. App name `Operation Sponsorships`, support email `pennstatemcvb@gmail.com`. **Next.**
3. Audience: choose **External**. **Next.**
4. Contact email `pennstatemcvb@gmail.com`. **Next.** Agree to the policy, **Continue**, **Create**.
5. **Audience**, then **Test users**, **Add users**: add `pennstatemcvb@gmail.com` (and nobody else needed). **Save.**
6. **Data Access**, **Add or Remove Scopes**: tick these three (use the filter box to find them), then **Update** and **Save**:
   - `.../auth/spreadsheets`
   - `.../auth/drive.file`
   - `.../auth/gmail.compose`

**3d. Create the OAuth client.**
1. Menu, **Google Auth Platform**, **Clients**, **Create Client**.
2. Application type: **Desktop app**. Name: `operation-sponsorships-cli`. **Create.**
3. In the list of clients, click the download icon for it to get the JSON file.
4. Save that file as **`config/credentials.json`** in this project. (It is gitignored and blocked by the pre-commit hook.
   It is not a password you can use to read your mail, but treat it as private anyway.)

**3e. Sign in.**
```bash
python main.py auth
```
A browser opens. Choose pennstatemcvb@gmail.com. Google will show **"Google hasn't verified this app"**: this is your own
app and that is expected. Click **Advanced**, then **Go to Operation Sponsorships (unsafe)**, then tick all three
permission boxes and **Continue**. If you sign in with any other account, nothing is saved and you are told why.

> **Do this to stop the 7-day logout.** While the consent screen is in **Testing**, Google expires the login every
> 7 days and you will need to run `python main.py auth` again. To stop that: **Google Auth Platform, Audience, Publish
> app** (move it to **In production**). Google documents the 7-day expiry only for apps in Testing. An unverified app
> shows the warning above and is limited to 100 users, which does not matter for a tool only the club uses; you just click
> through the warning. The program tells you when a login has expired.

### 4. Create the review sheet
```bash
python main.py init-sheet --share your.own@gmail.com
```
This creates the Google Sheet in the club account, writes the headers, dropdowns and colors, saves the sheet ID to
`.env`, and shares it with your own account (repeat `--share` for other officers). Open the link it prints.

### 5. Optional: lead-finding keys
Only needed for `discover`; `import` works without them. See [Finding leads](#finding-leads).

## Daily use

The same steps as the cheat sheet above, with the detail.

1. **Add leads.** `python main.py import --csv data/targets.csv` (contacts you already have) or
   `python main.py discover` (finds contacts at the brands you list).
2. **Draft.** `python main.py draft`. Each Pending row gets a subject and body, and `Approval Status` becomes
   `Pending Approval`.
3. **Review in the sheet.** Read each email. Edit the text right in the cell if you want. Set `Approval Status` to
   `Approved` or `Rejected`. You can do this from your phone.
4. **Create Gmail drafts.** `python main.py send`. Open Gmail as the club account, go to **Drafts**, read, press **Send**.
5. **Sync.** `python main.py send` marked those rows `Drafted`. After you send them, `python main.py sync` marks them `Sent`.
6. Any time: `python main.py status` shows where every row is and what to do next.

When you are comfortable, `python main.py send --live` sends for real (typed confirmation required). See
[How sending works](#how-sending-works).

## The review sheet

One Google Sheet is the queue. Columns are found by their header text, so you can reorder them.

| Column | Who fills it |
|---|---|
| Row ID | tool (stable key; safe to sort and filter the sheet) |
| Company Name, Category, Target Name, Target Email, Custom Hook / Angle | import, discover, or you |
| Generated Subject, Generated Body | drafter; then you edit freely |
| Approval Status | drafter sets `Pending Approval`; **you** set `Approved` or `Rejected` |
| Send Status | mailer: `Unsent`, `Sending`, `Drafted`, `Sent`, `Failed` |
| Sent Timestamp, Gmail ID | mailer (ISO time, Gmail's ID) |
| Notes | tool messages (why a row was skipped or blocked); your own lines are preserved |
| Company Domain | discover / import |

`Approval Status` is `Pending` (needs a draft), `Pending Approval` (draft ready for you), `Approved`, or `Rejected`.
Category is `Energy`, `Skincare`, `Hydration`, `Recovery`, or `Gear`. Anything else still works but gets a generic opening.

Custom Hook / Angle is yours: type a note such as *"they just launched a mineral SPF"* and the drafter uses it for the
opening instead of the default for that category.

How the sheet protects you:
- Every write re-finds the row by Row ID first, so sorting or filtering mid-run cannot hit the wrong lead.
- If you change a row while a run is in progress, the tool detects it and skips that row. Your edit wins.
- Only the cells a step owns are written; your edits elsewhere are never overwritten.
- Everything is written as plain text, so nothing can turn into a spreadsheet formula.
- You can paste rows in by hand. Row ID and statuses are filled in automatically on the next `draft` or `send`.

## Commands

| Command | What it does |
|---|---|
| `configure` | Save the model API key (hidden prompt) and optional keys to `.env`; tests the key. |
| `auth` | Sign in to Google as the club account. |
| `init-sheet` | Create the review sheet. `--repair` re-adds missing columns and dropdowns. `--share EMAIL` gives someone edit access. |
| `import --csv PATH` / `--sheet URL [--tab NAME]` | Add contacts that already have emails. Duplicates are skipped by address. |
| `discover [--dry-run]` | Find contacts at the brands in your list with Hunter or Apollo, then add them. `--limit N` brands per run, `--no-broad` (1 credit per brand at most), `--per-domain`, `--backend`, `--min-confidence`, `--max-reveals` (Apollo), `--recheck-days N`. |
| `try-draft [--company X] [--category Y] [--hook "..."] [--show-prompt]` | One sample email with the real model. Needs only the model key: no Google login, nothing saved or sent. |
| `draft [--dry-run] [--limit N] [--row ID]` | Write emails for `Pending` rows. `--dry-run` prints them without touching the sheet. |
| `send [--preview] [--limit N]` | Create Gmail drafts for `Approved` + `Unsent` rows. `--preview` changes nothing. |
| `send --live [--yes]` | Really send, with pauses. Asks you to type `SEND` unless `--yes`. |
| `sync` | Mark `Drafted` rows `Sent` once their draft has left Gmail Drafts. |
| `status` | Counts by stage, what is waiting on you, and what to run next. |
| `doctor [--online]` | Checks the setup and says exactly what is missing. `--online` tests the keys and logins and shows your Hunter credits. |

Exit codes: `0` fine, `1` something needs attention (the message says what), `2` unexpected error (details in
`logs/outreach.log`), `130` stopped with Ctrl+C. `-v` shows detailed logs.

## Safety

- **Approval gate.** Only rows with `Approval Status = Approved` and `Send Status = Unsent` are ever mailed.
- **Draft-first.** `send` creates Gmail drafts by default. Real sending needs `--live` and a typed confirmation.
- **Hard limits** (in `config/settings.py`, covered by tests): at most **15 emails per run**, and a random
  **180 to 420 second** pause between live sends. `MAX_EMAILS_PER_RUN` can lower the cap but never raise it.
- **Sender lock.** The mailer refuses to run unless the signed-in Google account is `pennstatemcvb@gmail.com`.
- **No double sends.** See [How sending works](#how-sending-works).
- **Public-repo guard.** `scripts/hooks/pre-commit` blocks `.env`, OAuth files, logs, lead CSVs, and anything that looks
  like an API key or token. Turn it on once per clone: `git config core.hooksPath scripts/hooks`. `doctor` warns if it is off.
- **Secrets stay local.** `.env` and `config/token.json` are written with owner-only permissions. Keys are never printed
  in full, never put in a URL, and never sent to any service except the one they belong to.
- **Privacy.** The model sees the company name, category, and your optional note. It never sees contact names or emails. On Gemini's free tier Google says it may use prompts to improve its products (see step 2 of Setup).

## How drafting works

`python main.py draft` takes every row that is `Pending` + `Unsent` and has a valid email.

1. The model writes only the subject and the middle of the email. Code adds the greeting, the opt-out line, the sign-off
   and the signature (name, title, email, both Instagram handles), so those can never drift or be misspelled.
2. Deterministic checks run on the result. A draft must contain the required sentence word for word (*"We have
   partnered with brands in the past and are always excited to showcase and actively promote products our athletes
   love."*), say top 10 / NCVF / 30 athletes, mention the campus groups and the season, make a clear ask, stay at 90 to 170
   words, and avoid banned brands (default: Celsius), dashes, semicolons, exclamation marks, emoji, markdown,
   placeholders, stock phrases, and any number except 10 and 30.
3. Easy problems are repaired automatically (long dashes become commas, a stray greeting or sign-off is trimmed).
   Anything else goes back to the model once with the exact list of problems.
4. A draft that still fails is **not** written. The row stays `Pending` and the reason goes in `Notes`.
5. A good draft is written with `Approval Status = Pending Approval`. Nothing is sent until you set `Approved`.

Rows are skipped, with a note, when there is no valid email, the company is on `BANNED_BRANDS`, or the row already has
draft text (clear the subject and body cells to have it rewritten).

The opening follows the category: skincare gets skin barrier and sun protection on outdoor tournament days, hydration
gets electrolytes across tournament weekends, recovery gets recovery between matches, energy gets caffeine for training
blocks, gear gets equipment that lasts a season. The season wording follows the calendar: August to September is the
middle of the fall slate, October to December is wrapping up fall and gearing up for spring, January to April is the
spring run toward regionals and nationals, May to July is planning the next season.

Set `LLM_PROVIDER=gemini` (and `GEMINI_API_KEY`) to use Gemini instead of Claude.

## How sending works

`python main.py send` looks only at rows that are `Approved` + `Unsent`.

| Mode | Command | What happens |
|---|---|---|
| Drafts (default) | `send` | A Gmail draft per row. Nothing is sent. Row becomes `Drafted`. |
| Live | `send --live` | Sends for real. Row becomes `Sent` with an ISO timestamp and the Gmail ID. |

A live run of 15 emails takes about 70 minutes (between 42 and 98) because of the 14 random pauses. Keep the window open and the Mac awake
(`caffeinate -i python main.py send --live`). Ctrl+C is safe at any point.

Protections against the worst failure, emailing a brand twice:
- The row is marked `Sending` *before* Gmail is called and re-checked at that moment, so a row you flipped to
  `Rejected` a second ago is not sent. Text edited at the last instant is not sent either.
- The send call is never repeated. If Gmail does not confirm (timeout, dropped connection, server error), the row stays
  in `Sending`, the run stops, and `Notes` tells you to check the Gmail Sent folder. Rows in `Sending` are never retried
  automatically: you set them to `Sent` or `Unsent` yourself.
- The same address is never mailed twice, across rows or within a run.
- Each row is checked again before sending: valid address, subject and body present, no leftover `[placeholders]`, no banned
  brand. A row that fails is skipped with the reason in `Notes` and goes out on a later run once fixed.
- Three failures in a row stop the run. A quota or login problem puts the row back to `Unsent` and stops.

A note on deliverability: a brand-new Gmail sender that suddenly mails many strangers can be rate limited or flagged.
Start small (a handful a day), and let the 15 per run cap be a ceiling rather than a target. Every email carries a plain
opt-out line.

## Finding leads

**Who fills in `data/targets.csv`? You do.** It is a short list of brands you want to approach, three columns:
`Company Name, Domain, Category` (Category is Energy, Skincare, Hydration, Recovery, or Gear). Copy
`data/targets.example.csv`, which already has 20 brands, and add or delete rows in Excel, Numbers, Google Sheets (download as
CSV), or any text editor. Good places to find more brands: ask the roster what they actually use, and look at who sponsors
other college and club teams.

`discover` does **not** find new brands. It finds the *people* (marketing and partnerships contacts) at the brands you list,
using Hunter or Apollo, and adds them to the review sheet as `Pending`. The file is only read when you run a command, so
nothing happens until you do.

Three ways to get leads in, all ending as new `Pending` rows (duplicates by email are skipped):

1. **A CSV you write.** Copy `data/targets.example.csv` to `data/targets.csv` (it is gitignored, so lead data never reaches
   GitHub). With a `Target Email` column, `import` adds those contacts directly.
2. **Another Google Sheet.** `python main.py import --sheet <url> --tab Targets` (share it with the club account first).
3. **Automatic lookup** with Hunter or Apollo: `python main.py discover`. List just `Company Name, Domain, Category` and it
   finds marketing and partnerships contacts. Targeted titles: Campus Marketing Manager, Field Marketing Manager, Sports
   Marketing Specialist, Grassroots Marketing Coordinator, Partnerships Manager (similar titles rank next; VPs, interns,
   and unrelated roles are dropped, as are generic inboxes like info@).

`data/targets.example.csv` already lists 20 brands across the five categories (energy, skincare, hydration, recovery,
gear). Celsius is deliberately not in it.

**Hunter** (default): key from <https://hunter.io/api-keys>, then `HUNTER_API_KEY=` in `.env`. Hunter's free plan includes
50 credits a month (check <https://hunter.io/pricing> for current numbers). A search costs 1 credit when it returns 1 to
10 emails and nothing when it returns none. Each brand takes one search for your five target titles; if none matches, a
second, wider marketing-and-communications search runs, so a brand costs 1 or 2 credits (`--no-broad` keeps it at 1).
`discover` tells you the worst case before it starts.

**Apollo**: key from <https://app.apollo.io/#/settings/integrations/api> (a master key, or one with the people-search and
enrichment routes enabled), then `APOLLO_API_KEY=` and
`DISCOVERY_BACKEND=apollo`. Searching is free but returns no emails; each revealed email costs about 1 credit, so only the
best few per brand are revealed and `--max-reveals` caps a run (default 20).

Always try `python main.py discover --dry-run` first: it lists what would be looked up and spends nothing. Before a real run
it also shows your actual Hunter balance (that account call is free), for example
`Your Hunter plan (Free): 50 of 50 credits left, resetting 2026-11-19.`, and it stops by itself when the credits run out.

Two things stop it paying twice. Brands already in the sheet are skipped. A brand that was searched but had no usable
contact (only VPs, generic inboxes, or nobody at all) is remembered in `data/lookups.json` (gitignored) and left alone for
30 days; `--recheck-days 0` searches everything again, and deleting that file does the same. If the plan runs out mid-run,
what was found is kept. Check each address before approving: public-data tools can be out of date.

## Troubleshooting

| You see | Do this |
|---|---|
| `This project needs Python 3.10 or newer` | Use the venv: `source .venv/bin/activate`, or `.venv/bin/python main.py ...` |
| `Not signed in to Google yet` | `python main.py auth` |
| `Google rejected the saved login` / login stopped working after a week | The consent screen is in Testing. Run `python main.py auth`, and publish the app (step 3e) to stop it recurring. |
| `Google hasn't verified this app` | Expected for your own app. **Advanced**, **Go to ... (unsafe)**. |
| `... API is not enabled in your Google Cloud project` | Open the link in the message, click **Enable**, wait a minute. |
| `You signed in as X, but this project sends from pennstatemcvb@gmail.com` | Run `auth` again and choose the club account. Nothing was saved. |
| `This is a 'Web application' client` | Create a **Desktop app** OAuth client instead (step 3d). |
| A row is stuck in `Sending` | Check Gmail's Sent folder for that address. If it is there set `Send Status` to `Sent`, otherwise `Unsent`. |
| Draft says `draft rejected after 2 tries` | Read `Notes`. Often a Custom Hook / Angle that conflicts with the rules. Edit it and run `draft` again. |
| `no credit left` / `rejected ANTHROPIC_API_KEY` | Add credit at <https://platform.claude.com/settings/billing> or create a new key, then `configure`. |
| Hunter says no searches left | Wait for the monthly reset, upgrade, or use `import` with your own contacts. |
| Anything else | `python main.py doctor --online`, then look at `logs/outreach.log` (private, gitignored). |

## Development

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # the whole suite, offline, a few seconds
.venv/bin/python -m pyflakes src config tests main.py
```

Tests use in-memory fakes for Sheets, Gmail, the model, and Hunter/Apollo; nothing touches the network (a few use local
sockets to check real HTTP behaviour). The safety-critical behaviours (no double sends, caps, delays, account lock,
approval gate, credit protection) were each checked by temporarily breaking the code and confirming a test fails.

```
config/    settings and hard limits, errors, logging, Google OAuth loader, Google error messages
src/       models, sheet_manager, llm, guardrails, drafter, mailer, discovery, cli
data/      targets.example.csv (your real lead files stay local and gitignored)
scripts/   hooks/pre-commit
tests/     pytest, with fakes
main.py    entry point
```
