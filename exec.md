# Running the app

A runbook: what to start, in what order, and what to do when something looks
stuck. Every command here is copy-pasteable from the project root:

```
C:\Users\AORUS\Documents\projects\job-hunt
```

`PY` below means `.venv\Scripts\python.exe`. Use that rather than a bare
`python`, which is a different interpreter without the project's packages.

---

## 1. Start it

Three things must be running. Only the first two are yours to start; PostgreSQL
runs as a Windows service and is usually already up.

### PostgreSQL — the data

Already running unless Windows says otherwise. To check:

```shell
powershell -Command "Get-Service postgresql-x64-14 | Select-Object Status"
```

Expect `Running`. If it is stopped:

```shell
powershell -Command "Start-Service postgresql-x64-14"
```

### Ollama — the model, needed only for translating and grading

```shell
ollama serve
```

Leave that window open: **closing it stops the server.** If you would rather not
think about it, install the Ollama desktop app, which starts with Windows and
keeps the server running in the tray.

Check it:

```shell
curl http://localhost:11434/api/tags
```

### The app — the web pages

```shell
.venv\Scripts\python.exe main.py
```

Leave that window open too. Then:

| Page | What it is |
|---|---|
| <http://localhost:8000/board> | every vacancy, sorted by fit, in English |
| <http://localhost:8000/companies> | every employer, with a Scrape button each |
| <http://localhost:8000/workspace> | raw listings (saving HTML only stores it; the list under the form shows what is **waiting to be graded**, with a Grade button per listing and a **Bulk grade** bar for up to 100 at a time - one after another on the GPU, pausing to cool down, safe to leave running):  choose **Process existing listings** (walks the collected URLs one by one, link prefilled, you paste its HTML; Skip leaves a link for later) or **Process other url** (blank form). The switch at the top moves between Jobs and Workspace |
| `python scripts\process_listing.py 1 2 3` | grade stored raw listings (by `raw_job_listings` id, or `--all`) on the local model and create real jobs from them; a removed posting is `not_found`, an ungradeable one stores nothing. Needs Ollama |
| <http://localhost:8000/workspace/urls> | paste a Google results `<div id="search">` block; the result links are stored once each. Each row has a **Process** button (opens the HTML form with that link; disabled once its HTML is stored) |
| <http://localhost:8000/jobs-test> | what the parsers stored in `jobs_test`, with a detail drawer |
| <http://localhost:8000/> | the DBOS workflow demo (original starter page) |

Stop either window with `Ctrl+C`.

---

## 2. The normal loop

### Find vacancies

Two ways, same pipeline underneath.

**From the employers page** (easiest): open `/companies`, find an employer,
press **Scrape**. It runs that employer's own Nationale Vacaturebank search,
stores what it finds, then translates and grades it. The row reports the result
and plays a chime when it finishes. Press it on several employers if you like -
the work queues up rather than piling onto the GPU.

**Many employers at once**: set the filters (e.g. **Last scraped before**
28/09/2026 - never-scraped employers count too unless you untick them), then in
**Batch scrape** enter how many and press **Scrape N**. It takes the first N
rows of the list as filtered and sorted. The page sorts by **Oldest added**
by default, so the employers that have been in the table longest go first. Each employer gets a progress line -
`fetching page 2/5`, `vacancy 5/23 - translating the description`, or `waiting
for the GPU` while another employer's vacancy is on it - and the chime plays
once, when the whole batch is done. **Cancel batch** stops everything, including
the grading it queued; what was already stored stays. Reloading the page picks
the progress back up.

**From the command line**, for a free-text search rather than one employer:

```shell
.venv\Scripts\python.exe scripts\ingest_jobs.py "Laravel" "Symfony" "PHP developer"
```

That only stores vacancies. Grade them with the next command.

### Translate and grade

```shell
.venv\Scripts\python.exe scripts\enrich_jobs.py --profile ahmed
```

Roughly **15-20 seconds per vacancy**, one at a time. Safe to stop with
`Ctrl+C` and run again later - finished vacancies are not redone.

Useful variants:

```shell
:: how far along, and how much GPU time has gone into it
:: (name the profile - grading is per profile and the numbers differ)
.venv\Scripts\python.exe scripts\enrich_jobs.py --progress --profile ahmed

:: just a few, to try it out
.venv\Scripts\python.exe scripts\enrich_jobs.py --profile ahmed --limit 5

:: one specific vacancy
.venv\Scripts\python.exe scripts\enrich_jobs.py --profile ahmed --job-id 42

:: redo work that is already current (after changing a prompt)
.venv\Scripts\python.exe scripts\enrich_jobs.py --profile ahmed --force
```

### Read the results

In the browser at `/board`, or in the terminal:

```shell
.venv\Scripts\python.exe scripts\enrich_jobs.py --shortlist --profile ahmed
```

Each line shows the score, the verdict and the eligibility tier:

```
 85  strong_match  RELOCATION  Back-end Developer
     HETWORKS - Veenendaal
```

Raise the bar with `--min-score 75`, and add `--include-blocked` to see the
good roles that are out of reach.

---

## 3. Reading the board

**Fit score** rates the work only - how well the vacancy matches your stack and
target roles. It never moves because of a visa or a language.

| Stack tier | Score band | Means |
|---|---|---|
| core | 75-95 | PHP, Laravel, Symfony, Livewire, Filament, Horizon |
| proficient | 55-75 | JavaScript, React, Node, Express |
| familiar | 30-55 | Python, Java, Ruby - touched, not production depth |
| other stack | 10-30 | a stack not in your profile |
| non-technical | 0-10 | not a software role |

**Eligibility** is a separate traffic light:

| | Means | What to do |
|---|---|---|
| 🟢 **remote OK** | doable from Tunisia today | apply |
| 🟠 **relocation** | on site or hybrid, advert silent on permits | apply - relocation and sponsorship are a conversation |
| 🔴 **blocked** | the advert itself rules you out: it demands an existing permit or residency, or fluent Dutch | skip |

Orange is not a rejection. Most Dutch adverts never mention permits, and those
land orange by design.

Filters worth knowing: set **Stack** to `core stack` and untick **Show blocked**
to get the list actually worth your time. **Language** keeps vacancies written in
English or Dutch. The list shows 50 vacancies per page (change it, or jump to a
page, in the pager under the list); changing any filter returns to page 1.

Each card has **Mark applied** and **Archive** buttons (also in the detail
drawer). Marked vacancies drop out of the default **Show: Open** view; switch
**Show** to `Applied`, `Archived` or `Everything` to see them again. Clicking a
mark a second time clears it.

---

## 4. Changing how vacancies are scored

Everything is scored against `profiles\ahmed.json`. Edit it - skills, target
roles, dealbreakers, education, what you are not interested in - then re-grade:

```shell
.venv\Scripts\python.exe scripts\enrich_jobs.py --profile ahmed --force
```

A different profile is just another file. `profiles\data-eng.json` is graded
with `--profile data-eng`, and its scores are stored separately, so the two
never mix.

---

## 5. What one click can cost

A single scrape is bounded on three axes, so a badly-matching employer name
cannot run away with your evening:

| Limit | Value | Why |
|---|---|---|
| pages walked | 20 | "Techniek" matches 22,321 vacancies across 893 pages |
| new vacancies stored | 50 | counts *new* rows only; re-seen ones are free |
| vacancies sent to the GPU | 50 | the rest are deferred to a later run |

The row tells you when a limit bit: *"59 found · 50 new (stopped at the 50-new
cap)"*.

**The search is full text**, so an employer whose name contains a common word
matches half the site - "7 Solutions" returns 287 vacancies, almost none of them
that employer's. Distinctive names behave well; generic ones bring noise.

---

## 6. When something looks stuck

### The page says "grading stopped: Ollama went away"

The server died partway through. The run cancelled whatever was still queued
rather than letting it fail one vacancy at a time. Start `ollama serve` and run
the enrichment again - everything scraped is kept, and the ungraded vacancies
are picked up where they were left.

### The page says "grading skipped: Ollama is not running"

Exactly what it says. Start `ollama serve` and run the enrichment again - the
vacancies were stored, only the grading was skipped, and nothing is lost.

### A scrape runs forever

Open that employer's row, press **Cancel** in the drawer. It stops the scrape
*and* the grading it queued. Whatever was already stored stays stored.

### Grading pauses for a few minutes at a time

That is the GPU cooldown: above 80°C the queue waits 3 minutes before the next
vacancy, and the progress line says so ("GPU at 84°C - cooling down for 3
min"). Tune it in `.env` with `GPU_MAX_TEMP_C`, `GPU_COOLDOWN_SECONDS` and
`GPU_MAX_COOLDOWNS` (it carries on after that many pauses regardless).

### Nothing is happening at all - no GPU, no progress

The grading queue allows one job at a time, and a job stranded by an older
version of the code can hold that single slot forever. Clear it:

```shell
.venv\Scripts\python.exe scripts\enrich_jobs.py --clear-stuck
```

Then run the enrichment again. This is the fix when `--progress` shows work
pending but nothing moves for minutes.

Caveat: this only sees work started by `enrich_jobs.py` itself. Work started
from the web app lives in a different DBOS database (see "Known gaps" in
`docs/architecture.md`). Before restarting the app on a new version, let its
current run finish, so nothing is stranded in the first place.

### Watching what it is doing

```shell
tail -f logs\dev.log
```

Every model call, every page fetched, every scrape result. Cleaner than the
terminal output.

---

## 7. Checking the machine is healthy

```shell
:: is anything listening where it should be?
curl http://localhost:8000/board
curl http://localhost:11434/api/tags

:: how much is done
.venv\Scripts\python.exe scripts\enrich_jobs.py --progress

:: does the code still pass its tests
.venv\Scripts\python.exe -m pytest -q
```

The last one needs PostgreSQL and takes about two minutes. It starts the app
itself for the browser tests, so stop your own copy first or let it use the one
already running.

---

## 8. Everything you ran, and how risky it was

Commands run against this project by an automated agent are recorded:

```shell
.venv\Scripts\python.exe tools\cmdlog.py report
.venv\Scripts\python.exe tools\cmdlog.py report --min-risk high
.venv\Scripts\python.exe tools\cmdlog.py verify
```

`verify` flags anything logged as less dangerous than it actually was.

---

## Quick reference

```shell
:: start (two windows)
ollama serve
.venv\Scripts\python.exe main.py

:: work
.venv\Scripts\python.exe scripts\ingest_jobs.py "Laravel"
.venv\Scripts\python.exe scripts\enrich_jobs.py --profile ahmed

:: look
start http://localhost:8000/board
.venv\Scripts\python.exe scripts\enrich_jobs.py --shortlist --profile ahmed

:: when stuck
.venv\Scripts\python.exe scripts\enrich_jobs.py --clear-stuck
```
