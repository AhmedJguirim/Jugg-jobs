"""Ingest companies.json into the companies table.

Operational script, not application logic: it is invoked by a human (or CI), not
imported by the app. It reuses the repository layer so ingestion and the rest of
the app agree on validation and conflict handling.

    python scripts/ingest_companies.py                    # ingest ./companies.json
    python scripts/ingest_companies.py --file other.json
    python scripts/ingest_companies.py --dry-run          # validate, write nothing
    python scripts/ingest_companies.py --preserve-ids     # keep the source ids

The load is idempotent: re-running updates existing rows (matched on `name`)
instead of failing on duplicate keys.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Iterable, Iterator

# Allow running as a plain script: python scripts/ingest_companies.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from app.repositories import company as company_repo  # noqa: E402
from app.schemas.company import CompanyCreate  # noqa: E402

DEFAULT_FILE = "companies.json"
BATCH_SIZE = 500

# Columns the source file may supply. Anything else is ignored with a warning,
# so an upstream schema change is visible rather than silently dropped.
KNOWN_FIELDS = {
    "id",
    "name",
    "type",
    "website",
    "slug",
    "province",
    "google_jobs_link",
    "linkedin_jobs_link",
    "indeed_jobs_link",
    "glassdoor_jobs_link",
    "nationalevacaturebank_jobs_link",
    "created_at",
    "updated_at",
}


def load_records(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array of objects")
    return data


def _batched(items: list[dict], size: int) -> Iterator[list[dict]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def normalise(
    records: Iterable[dict[str, Any]],
    *,
    preserve_ids: bool,
) -> tuple[list[dict], list[str], set[str]]:
    """Validate rows through CompanyCreate. Returns (rows, errors, unknown_fields)."""
    rows: list[dict] = []
    errors: list[str] = []
    unknown: set[str] = set()
    seen_names: dict[str, int] = {}

    for index, record in enumerate(records):
        unknown |= set(record) - KNOWN_FIELDS
        try:
            payload = CompanyCreate.model_validate(record)
        except ValidationError as error:
            errors.append(f"record #{index} (id={record.get('id')!r}): {error.errors()[0]['msg']}")
            continue

        row = payload.model_dump()

        # The upsert matches on `name`; duplicates inside one batch would make
        # PostgreSQL raise "cannot affect row a second time", so drop them here.
        if row["name"] in seen_names:
            errors.append(
                f"record #{index}: duplicate name {row['name']!r} "
                f"(first seen at #{seen_names[row['name']]}) - skipped"
            )
            continue
        seen_names[row["name"]] = index

        if preserve_ids and record.get("id") is not None:
            row["id"] = int(record["id"])
        rows.append(row)

    return rows, errors, unknown


def resync_identity_sequence() -> None:
    """Move the id sequence past the highest explicit id.

    Only needed after --preserve-ids: inserting explicit ids does not advance
    the sequence, so the next ORM insert would otherwise collide.
    """
    with session_scope() as session:
        session.execute(
            text(
                "SELECT setval("
                "  pg_get_serial_sequence('companies', 'id'),"
                "  COALESCE((SELECT MAX(id) FROM companies), 1)"
                ")"
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest companies.json into PostgreSQL.")
    parser.add_argument("--file", default=DEFAULT_FILE, help=f"source JSON (default: {DEFAULT_FILE})")
    parser.add_argument("--dry-run", action="store_true", help="validate only; write nothing")
    parser.add_argument(
        "--preserve-ids",
        action="store_true",
        help="keep the ids from the source file instead of letting the database assign them",
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args(argv)

    records = load_records(args.file)
    print(f"read {len(records)} record(s) from {args.file}")

    rows, errors, unknown = normalise(records, preserve_ids=args.preserve_ids)
    if unknown:
        print(f"WARNING: ignoring unknown source field(s): {sorted(unknown)}")
    for message in errors[:20]:
        print(f"  skipped: {message}")
    if len(errors) > 20:
        print(f"  ... and {len(errors) - 20} more skipped")
    print(f"validated {len(rows)} row(s), {len(errors)} skipped")

    if args.dry_run:
        print("dry run: nothing written")
        return 0
    if not rows:
        print("nothing to write")
        return 1

    written = 0
    with session_scope() as session:
        for batch in _batched(rows, args.batch_size):
            written += company_repo.bulk_upsert(session, batch)
            print(f"  upserted {written}/{len(rows)}", end="\r")
    print()

    if args.preserve_ids:
        resync_identity_sequence()
        print("id sequence resynced")

    with session_scope() as session:
        total = company_repo.count(session)
    print(f"done: {written} row(s) written, {total} row(s) now in companies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
