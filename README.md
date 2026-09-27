# job-hunt

A job-hunting tool for the Dutch market: tracks employers and pulls vacancies
from job boards using DBOS durable workflows, backed by PostgreSQL.

See [`docs/architecture.md`](docs/architecture.md) for the design, and
[`docs/adr/`](docs/adr/) for why each decision was made.

## Setup

1. **Dependencies**

   ```shell
   python -m venv .venv
   .venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
   pip install -r requirements.txt -r requirements-dev.txt
   playwright install chromium     # only needed for the e2e tests
   ```

2. **Configuration** - copy `.env.example` to `.env` and fill in your
   PostgreSQL credentials. `.env` is git-ignored.

3. **Databases**

   ```shell
   psql -U postgres -c "CREATE DATABASE nljobs;"
   psql -U postgres -c "CREATE DATABASE nljobs_test;"
   alembic upgrade head
   ```

4. **Seed data**

   ```shell
   python scripts/ingest_companies.py --preserve-ids
   ```

   Idempotent: re-running updates existing rows instead of duplicating them.

5. **Ingest vacancies**

   ```shell
   python scripts/ingest_jobs.py "Odido"                  # every result page
   python scripts/ingest_jobs.py "Odido" --limit 25 --max-pages 2
   python scripts/ingest_jobs.py "python developer" "data engineer"
   ```

   Or durably, through the running app:

   ```shell
   curl -X POST http://localhost:8000/jobs/ingest \
     -H 'Content-Type: application/json' \
     -d '{"query":"Odido"}'
   curl http://localhost:8000/jobs/ingest/<workflow_id>
   ```

   Safe to re-run: vacancies are keyed on `(source, external_id)`, so a second
   run updates rather than duplicates.

6. **Translate and grade them on your local LLM**

   Needs Ollama running with the model pulled (`ollama pull qwen3.5:4b`). If it
   is not running, scrapes still store their vacancies and simply report that
   grading was skipped - run the enrichment again once Ollama is back and they
   are picked up where they were left.

   ```shell
   python scripts/enrich_jobs.py                 # everything pending
   python scripts/enrich_jobs.py --limit 5       # a few, to try it out
   python scripts/enrich_jobs.py --job-id 42     # one vacancy
   python scripts/enrich_jobs.py --progress      # how far along, GPU time spent
   python scripts/enrich_jobs.py --shortlist     # the best matches, in English
   ```

   Vacancies are scored against `profiles/<name>.json`, selected by
   `LLM_DEFAULT_PROFILE`. Skills are tiered (`core` / `proficient` / `familiar`)
   and the tier fixes the score band, so a role on your daily stack cannot be
   outranked by one in a language you have only touched. Eligibility (Dutch,
   visa sponsorship, seniority gap) is flagged separately and never lowers the
   score - see `docs/adr/0008`.

   **One GPU job at a time.** All model work goes through the DBOS `llm` queue,
   registered with `worker_concurrency=1` and `global_concurrency=1`. The second
   limit is enforced in the database, so even starting this twice cannot put two
   models on your GPU. Edit `profiles/default.json` first - it is what every
   vacancy is scored against.

7. **Run**

   ```shell
   python main.py
   ```

   Then visit:

   - <http://localhost:8000/board> - the job board: what the flows produced,
     sorted by fit, with English translations and dealbreaker flags
   - <http://localhost:8000/companies> - browse all tracked employers, their
     vacancies, and their job-board search links. Each employer has a
     **Scrape vacancies** button that runs their own Nationale Vacaturebank
     search through the full pipeline (all pages, then translate and grade).
     Pressing it for several employers queues the work: enrichment still runs
     one vacancy at a time on the `llm` queue.
   - <http://localhost:8000/> - the DBOS workflow demo page

## Testing

```shell
pytest tests/unit -q          # fast; needs PostgreSQL (nljobs_test)
pytest tests/test_app.py -q   # end-to-end browser tests (Playwright)
pytest -q                     # everything
pytest -m "not e2e" -q        # skip the browser tests
```

## Development logging

Instead of reading the terminal, follow one clean stream:

```python
from app.logging_setup import log, section

section("parsing resumes")
log.info("value=%s", value)
```

Everything lands in `logs/dev.log`, including DBOS's own log lines. Follow it
with `tail -f logs/dev.log`.

## Command audit log

Every shell command run against this project by an automated agent is recorded
with a risk level (`none` - `low` - `medium` - `high` - `severe`):

```shell
python tools/cmdlog.py report                 # what has been run
python tools/cmdlog.py report --min-risk high # only the dangerous ones
python tools/cmdlog.py verify                 # flag under-reported risk
```

Trail: `logs/command_audit.log` (human) and `logs/command_audit.jsonl` (machine).

## Layout

```
main.py                     FastAPI routes + DBOS workflows
app/config.py               settings from env/.env
app/db/                     engine, session, models, base
app/repositories/           CRUD; all SQL lives here
app/services/               flows (paginating job ingest)
app/workflows/              DBOS workflows + queue registration (GPU serialised)
app/llm/                    prompts, profiles, translation, grading
app/text/                   HTML sanitising, language detection, chunking
profiles/                   candidate profiles for scoring
app/schemas/                pydantic DTOs (public contract)
app/integrations/nvb/       Nationale Vacaturebank client + response models
app/integrations/ollama/    local LLM client (the only GPU user)
app/api/                    read-only HTTP API for the views
html/board.html             job board view (Tailwind + Alpine)
html/companies.html         employers browser (Tailwind + Alpine)
app/logging_setup.py        file logger for development
alembic/                    migrations
scripts/                    operational one-shots (company + job ingest)
tools/cmdlog.py             command audit log
tests/unit/                 fast tests (PostgreSQL)
tests/test_app.py           end-to-end browser tests
docs/                       architecture + ADRs
```
