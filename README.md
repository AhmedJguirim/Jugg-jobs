# job-hunt

A job search tool that runs on your own machine. It collects vacancies, translates
the Dutch ones into English, and grades every one of them against your CV with a
small local language model. You end up with one board, sorted by how well each job
fits you, that tells you separately whether you can actually get the job.

It started as a tool for the Dutch market (Nationale Vacaturebank) and now also
reads postings from Ashby, Lever, Greenhouse, Workable and Breezy HR.

## Why you might want it

- **You don't read Dutch.** Most Dutch vacancies are written in Dutch. Every one
  is translated before you see it, and the original is kept.
- **Job boards rank badly.** Keyword search returns hundreds of results, and most
  of them have nothing to do with your stack. Here every vacancy gets a fit score
  against *your* profile: what you use daily, what you have used, what you have
  only touched.
- **"Good match" and "can I get it" are different questions.** A perfect role that
  demands fluent Dutch or an existing EU work permit is still a perfect role,
  just one you can't take. The board keeps the two apart: a fit score for the
  work, and a separate traffic light (blocked / needs relocation / remote OK) for
  eligibility.
- **It is free to run and private.** Grading uses [Ollama](https://ollama.com)
  with a 4B model on your own GPU. No API keys, no per-call cost, and your CV
  never leaves your computer.
- **It doesn't lose work.** Scraping and grading run as durable
  [DBOS](https://www.dbos.dev) workflows. If the app or Ollama crashes halfway
  through a batch, it picks up where it stopped instead of starting over or
  re-grading what was already done.

The scoring is careful about what a small model can be trusted with. Anything
checkable, like whether a vacancy's stack is one you know, or whether the advert
really says a permit is required, is decided in code. The model is only asked
for judgement. `docs/architecture.md` explains why.

## What you need

- **Python 3.10+**
- **PostgreSQL 14+**
- **Ollama**, with the model pulled: `ollama pull qwen3.5:4b`. A GPU is strongly
  recommended. It runs on CPU too, but a listing then takes minutes instead of
  about 15-20 seconds.
- Optional: an NVIDIA card with `nvidia-smi` on the path. The app reads the GPU
  temperature and pauses grading when the card runs hot. Without it, the check
  is skipped.

It was developed on Windows, so the commands below use Windows paths. On
Linux/macOS, use `.venv/bin/python` instead of `.venv\Scripts\python.exe`.

## Setup

```shell
git clone https://github.com/AhmedJguirim/job-hunt.git
cd job-hunt

python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your PostgreSQL credentials. The other
values have working defaults.

Create the databases and the schema:

```shell
psql -U postgres -c "CREATE DATABASE nljobs;"
.venv\Scripts\python.exe -m alembic upgrade head
```

Load the employer list: about 2,900 Dutch companies, each with search links
for the main job boards.

```shell
.venv\Scripts\python.exe scripts\ingest_companies.py --preserve-ids
```

### Write your profile

Every score is measured against a profile, so this step matters most. Copy the
template and make it yours:

```shell
copy profiles\default.json profiles\me.json
```

Then set `LLM_DEFAULT_PROFILE=me` in `.env`. Your own profile files are
git-ignored, so your CV details won't end up in a commit.

The parts that change the results the most:

- **Skills, in tiers.** The tier sets the score band, so a role on your daily
  stack can't be outranked by one in a language you've only read about:

  ```json
  "skills": {
    "core":       ["PHP", "Laravel", "PostgreSQL"],
    "proficient": ["JavaScript", "React"],
    "familiar":   ["Python", "Docker"]
  }
  ```

- **`languages`** (`fluent` / `none`) decides whether "Dutch required" blocks you.
- **`location`**: `based_in`, `work_authorization`, `relocation` and
  `remote_preference`, in plain sentences. Eligibility is worked out from these.
- **`dealbreakers`** and **`not_interested_in`**: things that should sink a
  vacancy no matter how well the stack matches.
- **`education`**: Dutch adverts ask for a *level* (MBO / HBO / WO), not a
  degree. List the levels your degree meets, or the model will flag "HBO
  required" as something you lack.

You can keep several profiles (`--profile data-eng`, for example). Scores are
stored per profile and never mix.

## Using it

Start Ollama and the app, each in its own terminal:

```shell
ollama serve
.venv\Scripts\python.exe main.py
```

The app serves on <http://localhost:8000>. It has no login and listens on all
interfaces, so don't run it on a network you don't trust.

There are two ways to get vacancies in. Both end up on the same board.

### 1. Dutch vacancies (Nationale Vacaturebank)

**Per employer:** open <http://localhost:8000/companies>, find an employer and
press **Scrape**. The app runs that employer's search, stores the vacancies, then
translates and grades them one at a time. To do many at once, filter the list
(for example, "last scraped before" a date) and use **Batch scrape**. Each
employer gets its own progress line, and **Cancel batch** stops everything
cleanly.

**By keyword**, from the command line:

```shell
.venv\Scripts\python.exe scripts\ingest_jobs.py "Laravel" "python developer"
.venv\Scripts\python.exe scripts\enrich_jobs.py
```

The first command only stores vacancies. The second translates and grades
everything that hasn't been graded yet. You can stop it with Ctrl+C and run it
again later; finished vacancies are not redone. Useful flags: `--limit 5` to try
a few, `--progress` to see how far along it is, `--shortlist` to print the best
matches in the terminal.

A single click is capped (20 result pages, 50 new vacancies, 50 sent to the
model), so a generic employer name can't tie up your GPU for the whole evening.

### 2. Postings on Ashby, Lever, Greenhouse, Workable and Breezy HR

These come in through the **Workspace**, in three steps.

**Collect links.** Search Google for postings on those sites, for example:

```
site:jobs.lever.co ("Backend Developer" AND "Remote")
site:jobs.ashbyhq.com ("PHP Developer" AND "Remote")
site:apply.workable.com ("Laravel Developer" AND "Remote")
```

In the browser's dev tools, copy the `<div id="search">` element of the results
page and paste it into <http://localhost:8000/workspace/urls>. The links are
extracted and stored, each one only once, so pasting overlapping searches is fine.

**Fetch the pages.** Press **Process all** on the same page. It fetches each
waiting link's posting (up to 500 per click, with a short random pause between
requests) and puts it on the waiting list. Nothing is graded yet. Links that
were blocked stay in the list and are retried on the next click. You can also do
it by hand: **Process** on a row opens a form where you paste the posting's HTML
yourself.

**Grade.** Open <http://localhost:8000/workspace>. The list there is everything
waiting to be graded. Press **Grade** on one listing, or **Bulk grade** to queue
up to 100 and walk away. Graded postings become vacancies on the board.

### 3. Read the board

Open <http://localhost:8000/board>. Every vacancy shows, in English:

- **Fit score (0-100)** for the work alone, banded by how well the stack matches
  your profile:

  | Stack | Band |
  |---|---|
  | your core stack | 75-95 |
  | proficient | 55-75 |
  | familiar | 30-55 |
  | a stack you don't list | 10-30 |
  | not a software job | 0-10 |

- **Eligibility**, separate from the score:
  - **remote OK**: you can do it from where you are.
  - **relocation**: on site or hybrid, and the advert says nothing about
    permits. Most Dutch adverts never do, so treat this one as worth a try,
    not as a no.
  - **blocked**: the advert itself rules you out (demands an existing permit,
    residency, or fluent Dutch).

A good default view: set **Stack** to *core stack* and untick **Show blocked**.
Mark vacancies **Applied** or **Archive** them to get them out of the way.

If you change your profile, grade again with
`scripts\enrich_jobs.py --profile me --force`.

### When something looks stuck

[`exec.md`](exec.md) is the operator runbook: what to start, how to read the
progress lines, and what to do when grading stops. The most common case is work
pending with no GPU activity and no errors. A job left behind by an older
version of the code is holding the single model slot. Run
`scripts\enrich_jobs.py --clear-stuck`.

Everything the app does is logged to `logs/dev.log`.

## How it's built

FastAPI serves the pages, DBOS runs the workflows, PostgreSQL holds the data,
and the frontend is plain HTML with Tailwind and Alpine.js. There is one
constraint the design is built around: **one GPU, one model, one call at a
time.** Every model call goes through a single DBOS queue limited to one worker
across all processes. Page fetching runs on separate queues, so scraping never
waits on grading.

- [`docs/architecture.md`](docs/architecture.md): layout, data model, pipelines.
- [`docs/adr/`](docs/adr/): one short record per design decision and the
  reasoning behind it.

## Contributing

Issues and pull requests are welcome. Parsers for more applicant-tracking
systems (Teamtailor, Recruitee, Personio, ...) are the most useful addition.

Before you open a PR:

- **Run the tests.** They need PostgreSQL and a `nljobs_test` database (they
  skip cleanly without it):

  ```shell
  .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
  .venv\Scripts\python.exe -m playwright install chromium   # only for e2e
  .venv\Scripts\python.exe -m pytest tests/unit -q   # fast, no browser
  .venv\Scripts\python.exe -m pytest -q              # also the Playwright e2e tests
  ```

  The e2e tests use the app on port 8000 if it is running, and start it
  otherwise.

- **Schema changes go through Alembic** (`alembic revision --autogenerate`).
  A test fails if the models and migrations drift apart.
- **Changed a prompt?** Bump its version in `app/llm/prompts.py`. That is how
  existing translations and grades get marked stale.
- **Changed a workflow's steps?** Bump `APPLICATION_VERSION` in
  `app/workflows/enrichment.py`, or DBOS will fail to replay workflows that were
  in flight.
- **SQL lives in `app/repositories/`** and nowhere else.
- **A real design decision gets an ADR**: a new numbered file in `docs/adr/`.
  Don't edit accepted ones. Update `docs/architecture.md` in the same PR when
  you change something it describes.
- **Adding a platform:** look at an existing parser in `app/parsers/` and its
  fixtures in `tests/fixtures/`. New parsers write to the `jobs_test` table
  first (visible at `/jobs-test`) and are only wired into the real flow once
  their output checks out.

[`CLAUDE.md`](CLAUDE.md) has the same rules in more detail, plus the gotchas
that tend to cost people an afternoon. It is written for AI coding assistants,
but it's worth reading even if you don't use one.
