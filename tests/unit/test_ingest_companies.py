"""Unit tests for the companies ingest script's pure logic.

These cover normalisation only - no database is touched, so they run anywhere.
"""

from __future__ import annotations

import json

import pytest

from scripts.ingest_companies import load_records, normalise


@pytest.fixture
def record() -> dict:
    return {
        "id": 1,
        "name": "@Fentures B.V.",
        "province": "Amsterdam ",
        "google_jobs_link": "https://www.google.com/search?q=%40Fentures+jobs",
        "linkedin_jobs_link": "https://www.linkedin.com/jobs/search/?keywords=%40Fentures",
        "indeed_jobs_link": "https://nl.indeed.com/jobs?q=%40Fentures",
        "glassdoor_jobs_link": "https://www.glassdoor.com/Search/results.htm?keyword=%40Fentures",
        "nationalevacaturebank_jobs_link": "https://www.nationalevacaturebank.nl/vacature/zoeken",
        "created_at": "2026-07-11T11:51:24.000000Z",
        "updated_at": "2026-07-11T11:51:24.000000Z",
    }


class TestNormalise:
    def test_valid_record_is_kept(self, record: dict):
        rows, errors, unknown = normalise([record], preserve_ids=False)

        assert len(rows) == 1
        assert errors == []
        assert unknown == set()

    def test_trailing_whitespace_in_province_is_trimmed(self, record: dict):
        rows, _, _ = normalise([record], preserve_ids=False)

        assert rows[0]["province"] == "Amsterdam"

    def test_null_province_is_preserved_as_none(self, record: dict):
        rows, _, _ = normalise([{**record, "province": None}], preserve_ids=False)

        assert rows[0]["province"] is None

    def test_source_timestamps_are_not_written(self, record: dict):
        """created_at/updated_at are owned by the database, not the source file."""
        rows, _, _ = normalise([record], preserve_ids=False)

        assert "created_at" not in rows[0]
        assert "updated_at" not in rows[0]

    def test_preserve_ids_keeps_source_id(self, record: dict):
        rows, _, _ = normalise([record], preserve_ids=True)

        assert rows[0]["id"] == 1

    def test_without_preserve_ids_the_id_is_dropped(self, record: dict):
        rows, _, _ = normalise([record], preserve_ids=False)

        assert "id" not in rows[0]

    def test_missing_name_is_reported_not_raised(self, record: dict):
        broken = {k: v for k, v in record.items() if k != "name"}

        rows, errors, _ = normalise([broken], preserve_ids=False)

        assert rows == []
        assert len(errors) == 1

    def test_duplicate_names_in_one_batch_are_skipped(self, record: dict):
        """A repeated conflict target would break ON CONFLICT DO UPDATE."""
        rows, errors, _ = normalise([record, {**record, "id": 2}], preserve_ids=False)

        assert len(rows) == 1
        assert len(errors) == 1
        assert "duplicate name" in errors[0]

    def test_unknown_source_fields_are_reported(self, record: dict):
        rows, _, unknown = normalise([{**record, "surprise": 1}], preserve_ids=False)

        assert unknown == {"surprise"}
        assert len(rows) == 1  # still ingested, just flagged

    def test_empty_input(self):
        assert normalise([], preserve_ids=False) == ([], [], set())


class TestLoadRecords:
    def test_reads_a_json_array(self, tmp_path):
        path = tmp_path / "companies.json"
        path.write_text(json.dumps([{"name": "A"}]), encoding="utf-8")

        assert load_records(str(path)) == [{"name": "A"}]

    def test_rejects_a_non_array_document(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"name": "A"}), encoding="utf-8")

        with pytest.raises(ValueError):
            load_records(str(path))
