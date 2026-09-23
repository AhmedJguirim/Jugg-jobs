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

6. **Run**

   ```shell
   python main.py
   ```

   Then visit <http://localhost:8000>.

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
app/schemas/                pydantic DTOs (public contract)
app/integrations/nvb/       Nationale Vacaturebank client + response models
app/logging_setup.py        file logger for development
alembic/                    migrations
scripts/                    operational one-shots (company + job ingest)
tools/cmdlog.py             command audit log
tests/unit/                 fast tests (PostgreSQL)
tests/test_app.py           end-to-end browser tests
docs/                       architecture + ADRs
```
