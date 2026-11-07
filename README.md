# Jugg Jobs

A job search tool that runs on your own machine. You give it links to job
postings on Ashby, Lever, Greenhouse, Workable and Breezy HR. It fetches each
posting and grades it against your CV with a small local language model. You end
up with one board, sorted by how well each job fits you, that tells you
separately whether you can actually get the job.

## Why you might want it

- **Searching job boards is noisy.** A search for "backend developer remote"
  returns hundreds of postings, and most have nothing to do with your stack. Here
  every posting gets a fit score against *your* profile: what you use daily, what
  you have used, and what you have only touched.
- **"Good match" and "can I get it" are different questions.** A perfect role
  that is "remote, US only" or demands an existing work permit is still a perfect
  role, just one you can't take. The board keeps the two apart: a fit score for
  the work, and a separate traffic light (blocked / needs relocation / remote OK)
  for eligibility, based on where you live and where you're allowed to work.
- **You don't copy postings by hand.** Paste a page of Google results and the app
  pulls out the links, fetches every posting, and keeps only the part that
  matters.
- **It is free to run and private.** Grading uses [Ollama](https://ollama.com)
  with a 4B model on your own GPU. No API keys, no per-call cost, and your CV
  never leaves your computer.
- **It doesn't lose work.** Fetching and grading run as durable
  [DBOS](https://www.dbos.dev) workflows. If the app or Ollama crashes halfway
  through a batch, it picks up where it stopped instead of starting over or
  re-grading what was already done.

The scoring is careful about what a small model can be trusted with. Anything
checkable, like whether a posting's stack is one you know, or whether the advert
really restricts where you can work from, is decided in code. The model is only
asked for judgement. [`docs/architecture.md`](docs/architecture.md) explains how.

## What you need

- **Python 3.10+**
- **PostgreSQL 14+**
- **Ollama**, with the model pulled: `ollama pull qwen3.5:4b`. A GPU is strongly
  recommended. It runs on CPU too, but a posting then takes minutes to grade
  instead of seconds.
- Optional: an NVIDIA card with `nvidia-smi` on the path. The app reads the GPU
  temperature and pauses grading when the card runs hot. Without it, the check
  is skipped.

It was developed on Windows, so the commands below use Windows paths. On
Linux/macOS, use `.venv/bin/python` instead of `.venv\Scripts\python.exe`.

## Setup

```shell
git clone https://github.com/AhmedJguirim/jugg-jobs.git
cd jugg-jobs

python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your PostgreSQL credentials. The other
values have working defaults.

Create the database and the schema:

```shell
psql -U postgres -c "CREATE DATABASE jugg_jobs;"
.venv\Scripts\python.exe -m alembic upgrade head
```

### Write your profile

Every score is measured against a profile, so this step matters most. Copy the
example and make it yours:

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
    "proficient": ["JavaScript", "Vue", "Docker"],
    "familiar":   ["Python", "React"]
  }
  ```

- **`location`** and **`citizenship`**: `based_in`, `work_authorization`,
  `relocation` and `remote_preference`, in plain sentences. Eligibility is worked
  out from these, so be specific. For example: "Based in Tunisia. Can work
  remotely as a contractor or through an Employer of Record. On-site roles need
  visa sponsorship."
- **`languages`** (`fluent` / `basic` / `none`) decides whether a language
  requirement blocks you.
- **`dealbreakers`** and **`not_interested_in`**: things that should sink a
  posting no matter how well the stack matches.

You can keep several profiles. Scores are stored per profile and never mix.

## Using it

Start Ollama and the app, each in its own terminal:

```shell
ollama serve
.venv\Scripts\python.exe main.py
```

Open <http://localhost:8000>. The app has no login and listens on all
interfaces, so don't run it on a network you don't trust.

The navigation follows the three steps: **Listing URLs**, **Raw listings**,
**Job board**.

### 1. Collect links

Search Google for postings on the supported sites, for example:

```
site:jobs.lever.co ("Backend Developer" AND "Remote")
site:jobs.ashbyhq.com ("PHP Developer" AND "Remote")
site:job-boards.greenhouse.io ("Laravel Developer" AND "Remote")
site:apply.workable.com ("Symfony Developer" AND "Remote")
site:breezy.hr ("Backend Developer" AND "Remote")
```

In the browser's dev tools, copy the `<div id="search">` element of the results
page and paste it into **Listing URLs**. The links are extracted and stored, each
one only once, so pasting overlapping searches is fine. Go through a few result
pages and a few queries; the list grows quickly.

To copy the element: right-click a result, choose **Inspect**, find the
`<div id="search">` that wraps all the results in the Elements panel, then
right-click it and choose **Copy → Copy outerHTML**.

**Why this step is manual.** Google shows a CAPTCHA almost at once to anything
that searches automatically, whether that is plain HTTP requests or a headless
browser. I didn't find a reliable way around it that wasn't either fragile or a
paid search API. Copying the results by hand takes a few seconds per page, and
your normal browser session never trips the bot check. Everything after this
step is automated. If you know a clean way to automate it, a pull request is
very welcome.

### 2. Fetch the postings

Press **Process all** on the same page. It fetches each waiting link's posting
(up to 500 per click, with a short random pause between requests) and puts it on
the waiting list. Nothing is graded yet. Links that were blocked stay in the
list and are retried on the next click; postings that were taken down are
marked as such.

If a site refuses the request, you can do it by hand: **Process** on a row opens
a form where you paste the posting's HTML yourself.

### 3. Grade

Open **Raw listings** and pick either option. Under the form is the waiting
list: everything fetched but not graded yet. Press **Grade** on one posting, or
**Bulk grade** to queue up to 100 and walk away. They run one at a time on the
GPU, with cooldown pauses if it gets hot. Graded postings appear on the board.

### Read the board

Every posting on the **Job board** shows:

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
  - **relocation**: on site or hybrid, and the advert says nothing that rules
    you out. Worth applying to; sponsorship is a conversation.
  - **blocked**: the advert itself rules you out (for example, it hires only in
    countries you don't live in, or needs a language you don't speak).

A good default view: set **Stack** to *core stack* and untick **Show blocked**.
Mark postings **Applied** or **Archive** them to get them out of the way.

Changed your profile? Grade again with
`.venv\Scripts\python.exe scripts\process_listing.py --all --force`.

### When something looks stuck

[`docs/operations.md`](docs/operations.md) covers what each page does and what to
do when grading stops. The most common case is listings waiting with no GPU
activity and no errors: a job left behind by an older version of the code is
holding the single model slot. Stop the app and run
`.venv\Scripts\python.exe scripts\clear_stuck.py`.

Everything the app does is logged to `logs/dev.log`.

## How it's built

FastAPI serves the pages, DBOS runs the workflows, PostgreSQL holds the data,
and the frontend is plain HTML with Tailwind and Alpine.js. There is one
constraint the design is built around: **one GPU, one model, one call at a
time.** Every model call goes through a single DBOS queue limited to one worker
across all processes. Fetching runs on its own queue, so it never waits on
grading.

- [`docs/architecture.md`](docs/architecture.md): the flow, the layers, the data
  model, and how a posting is graded.
- [`docs/adr/`](docs/adr/): one short record per design decision and the
  reasoning behind it.

The project started as a tool for Dutch job boards, scraping and translating
vacancies from Nationale Vacaturebank. That part has been retired. If you're
interested in it, email me at
[jguirimahmed111@gmail.com](mailto:jguirimahmed111@gmail.com).

## Contributing

Issues and pull requests are welcome. Parsers for more applicant-tracking
systems (Teamtailor, Recruitee, Personio, ...) are the most useful addition.

Before you open a PR:

- **Run the tests.** They need PostgreSQL and a `jugg_jobs_test` database (they
  skip cleanly without it):

  ```shell
  .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
  .venv\Scripts\python.exe -m playwright install chromium   # only for e2e
  .venv\Scripts\python.exe -m pytest tests/unit -q          # fast, no browser
  .venv\Scripts\python.exe -m pytest -q                     # also the Playwright e2e tests
  ```

  The e2e tests use the app on port 8000 if it is running, and start it
  otherwise.

- **Schema changes go through Alembic** (`alembic revision --autogenerate`).
  A test fails if the models and migrations drift apart.
- **Changed a prompt?** Bump `GLOBAL_ASSESSMENT_PROMPT_VERSION` in
  `app/llm/global_prompts.py`. That is how existing grades get marked stale.
- **Changed a workflow's steps?** Bump `APPLICATION_VERSION` in
  `app/workflows/llm_queue.py`, or DBOS will fail to replay workflows that were
  in flight.
- **SQL lives in `app/repositories/`** and nowhere else.
- **A real design decision gets an ADR**: a new numbered file in `docs/adr/`.
  Don't edit accepted ones. Update `docs/architecture.md` in the same PR when
  you change something it describes.
- **Adding a platform:** add a parser in `app/parsers/` (it gets the page's HTML
  and returns a `JobCreate`), register its host in `PLATFORM_HOSTS`, teach
  `app/integrations/listing_pages/extract.py` which element of the page to keep,
  and add real pages to `tests/fixtures/<platform>/` with tests that parse them.
  The existing five are the template.

[`CLAUDE.md`](CLAUDE.md) has the same rules in more detail, plus the gotchas
that tend to cost people an afternoon. It is written for AI coding assistants,
but it's worth reading even if you don't use one.

## License

MIT - see [LICENSE](LICENSE).
