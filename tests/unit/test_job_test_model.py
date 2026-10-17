"""`jobs_test` must stay an exact copy of `jobs`' own columns and keys.

It is a scratch table for parser output, so any column added to `jobs` has to be
added here too (and migrated) or parsed jobs would silently lose the field.
"""

from __future__ import annotations

from app.db.models.job import Job
from app.db.models.job_test import JobTest


def column_shape(table):
    return {
        column.name: (
            repr(column.type),
            column.nullable,
            column.primary_key,
            str(column.server_default.arg) if column.server_default is not None else None,
            sorted(fk.target_fullname for fk in column.foreign_keys),
        )
        for column in table.columns
    }


def index_shape(table):
    return {
        # Names differ by table; what matters is columns, uniqueness and method.
        (tuple(c.name for c in index.columns), index.unique, index.dialect_options["postgresql"]["using"])
        for index in table.indexes
    }


def unique_shape(table):
    from sqlalchemy import UniqueConstraint

    return {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_same_columns():
    assert column_shape(JobTest.__table__) == column_shape(Job.__table__)


def test_same_indexes():
    assert index_shape(JobTest.__table__) == index_shape(Job.__table__)


def test_same_unique_keys():
    assert unique_shape(JobTest.__table__) == unique_shape(Job.__table__) == {("source", "external_id")}
