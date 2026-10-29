from __future__ import annotations

from pathlib import Path

from ats_finder.db import Database, SCHEMA_VERSION


def test_opening_a_v2_database_upgrades_it_and_adds_repost_link_indexes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade.sqlite3"
    database = Database(path)
    try:
        with database.transaction() as connection:
            for index in (
                "idx_jobs_repost_link",
                "idx_observations_repost_link",
                "idx_job_events_repost_link",
            ):
                connection.execute(f"DROP INDEX IF EXISTS {index}")
            connection.execute(
                "UPDATE schema_metadata SET value = '2' WHERE key = 'schema_version'"
            )
    finally:
        database.close()

    upgraded = Database(path)
    try:
        assert upgraded.schema_version == SCHEMA_VERSION
        with upgraded.read() as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
    finally:
        upgraded.close()

    assert {
        "idx_jobs_repost_link",
        "idx_observations_repost_link",
        "idx_job_events_repost_link",
    } <= names


def test_opening_current_schema_succeeds_while_another_connection_is_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent.sqlite3"
    writer = Database(path)
    try:
        with writer.transaction():
            reader = Database(path, timeout_seconds=0.1)
            try:
                assert reader.schema_version == SCHEMA_VERSION
                assert reader.list_sources() == []
            finally:
                reader.close()
    finally:
        writer.close()
