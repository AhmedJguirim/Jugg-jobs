# Running it

What to start, what each page does, and what to do when something looks stuck.
Setup is in the [README](../README.md). Commands are run from the project root;
on Linux/macOS use `.venv/bin/python` instead of `.venv\Scripts\python.exe`.

## Start

Three things must be running:

| What | How | Check |
|---|---|---|
| PostgreSQL | usually a system service | `psql -U postgres -c "select 1"` |
| Ollama | `ollama serve`, or the desktop app | `curl http://localhost:11434/api/tags` lists `qwen3.5:4b` |
| The app | `.venv\Scripts\python.exe main.py` | <http://localhost:8000> opens the board |

Stop the app with `Ctrl+C`. Work in flight is not lost: fetching and grading are
durable workflows, and they resume from the last finished step on the next start.

## The pages

| Page | What it does |
|---|---|
| **Listing URLs** (`/workspace/urls`) | Paste a Google results `<div id="search">` block; its result links are stored, each once. **Process all** fetches every waiting link on a supported site (up to 500 per click, 1-3 s random pause between requests) and puts the page on the waiting list. Blocked links stay and are retried next time; removed postings are marked as such. **Process** on a row opens the paste form for that link instead. |
| **Raw listings** (`/workspace`) | Choose **Process existing listings** (walk the collected links one by one and paste each page's HTML) or **Process other url** (a blank form). Under either form is the **waiting list**: a **Grade** button per listing, **Bulk grade** for up to 100 at a time, and **Cleanup** to empty the stored HTML of graded listings. |
| **Job board** (`/board`) | Every graded posting, sorted by fit. Filter by stack tier, minimum score, city, and whether to show blocked ones; mark postings **Applied** or **Archive** them. |

A bulk grade runs one listing at a time on the GPU and is safe to leave running.
It can be cancelled, and the page picks its progress back up after a reload.

## From the command line

```shell
:: grade stored raw listings by id, or all of them
.venv\Scripts\python.exe scripts\process_listing.py 12 13 14
.venv\Scripts\python.exe scripts\process_listing.py --all --profile me
```

Do not run it while the app is grading: it keeps its own DBOS state, so the
one-job-on-the-GPU limit does not span the two.

## Changing how postings are scored

Edit your profile (`profiles/<you>.json`) and grade again; listings with a
current grade are skipped unless forced:

```shell
.venv\Scripts\python.exe scripts\process_listing.py --all --force
```

Several profiles can coexist. Grades are stored per profile, and the board
shows the one `LLM_DEFAULT_PROFILE` names.

## When something looks stuck

**"Ollama is not running" / the model is missing.** Start `ollama serve` or
`ollama pull qwen3.5:4b`, then press Grade again. Nothing was lost: a listing
that could not be graded stays on the waiting list.

**Grading pauses for minutes at a time.** That is the GPU cooldown: above 80°C
the queue waits 3 minutes before the next listing, and the progress line says
so. Tune it with `GPU_MAX_TEMP_C`, `GPU_COOLDOWN_SECONDS` and
`GPU_MAX_COOLDOWNS` in `.env`.

**Listings are waiting, nothing is happening, no errors.** A workflow left
behind by an older version of the code is holding the single GPU slot. Stop the
app, then:

```shell
.venv\Scripts\python.exe scripts\clear_stuck.py --dry-run   :: see what it would cancel
.venv\Scripts\python.exe scripts\clear_stuck.py
```

**A listing says "Retry".** Its page could not be read as a job. Try again, or
**Remove link** if the page is not a posting (a careers index, a dead company
page).

**Watching what it does.** Everything - each request, each model call, each
grade - goes to `logs/dev.log`:

```shell
powershell -Command "Get-Content logs\dev.log -Wait -Tail 50"   :: Windows
tail -f logs/dev.log                                            :: Linux/macOS
```
