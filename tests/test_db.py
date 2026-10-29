from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from ats_finder.db import Database, SCHEMA_VERSION, datetime_to_storage
from ats_finder.models import CanadaPresence, SourceInput, SourceSupportStatus

from conftest import FIXED_NOW, EngineHarness


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


def test_opening_a_v3_database_adds_the_source_exclusions_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade_v4.sqlite3"
    database = Database(path)
    try:
        with database.transaction() as connection:
            connection.execute("DROP TABLE IF EXISTS source_exclusions")
            connection.execute(
                "UPDATE schema_metadata SET value = '3' WHERE key = 'schema_version'"
            )
    finally:
        database.close()

    upgraded = Database(path)
    try:
        assert upgraded.schema_version == SCHEMA_VERSION
        with upgraded.read() as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'source_exclusions'"
            ).fetchone()
        assert row is not None
    finally:
        upgraded.close()


def test_opening_a_v4_database_adds_canada_presence(tmp_path: Path) -> None:
    path = tmp_path / "upgrade_v5.sqlite3"
    database = Database(path)
    try:
        with database.transaction() as connection:
            connection.execute("ALTER TABLE ats_sources DROP COLUMN canada_presence")
            connection.execute(
                "UPDATE schema_metadata SET value = '4' WHERE key = 'schema_version'"
            )
    finally:
        database.close()

    upgraded = Database(path)
    try:
        assert upgraded.schema_version == SCHEMA_VERSION
        with upgraded.read() as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(ats_sources)")
            }
        assert "canada_presence" in columns
    finally:
        upgraded.close()


def test_classify_canada_presence_defers_only_non_canadian_boards(
    harness: EngineHarness,
    job_factory,
) -> None:
    paris = harness.database.upsert_source(
        SourceInput(
            provider="greenhouse",
            company="Paris Co",
            board_identifier="paris-co",
            career_page_url="https://boards.greenhouse.io/paris-co",
            support_status=SourceSupportStatus.SUPPORTED,
        ),
        now=FIXED_NOW,
    )
    empty = harness.database.upsert_source(
        SourceInput(
            provider="greenhouse",
            company="Empty Co",
            board_identifier="empty-co",
            career_page_url="https://boards.greenhouse.io/empty-co",
            support_status=SourceSupportStatus.SUPPORTED,
        ),
        now=FIXED_NOW,
    )
    harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("ottawa-job")],
        completed_at=FIXED_NOW,
        next_scan_at=FIXED_NOW + timedelta(hours=1),
    )
    harness.engine.process_complete_scan(
        paris.id,
        [job_factory("paris-job", location="Paris, France")],
        completed_at=FIXED_NOW,
        next_scan_at=FIXED_NOW + timedelta(hours=1),
    )
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE ats_sources SET next_scan_at = ? WHERE id = ?",
            (datetime_to_storage(FIXED_NOW + timedelta(hours=1)), paris.id),
        )

    classified = harness.database.classify_canada_presence(
        now=FIXED_NOW,
        recheck_after=timedelta(days=30),
    )

    ottawa = harness.database.get_source(harness.source.id)
    deferred = harness.database.get_source(paris.id)
    unknown = harness.database.get_source(empty.id)
    assert classified == {"some": 1, "none": 1, "unknown": 1}
    assert ottawa is not None
    assert ottawa.canada_presence is CanadaPresence.SOME
    assert ottawa.next_scan_at == FIXED_NOW + timedelta(hours=1)
    assert deferred is not None
    assert deferred.canada_presence is CanadaPresence.NONE
    assert deferred.next_scan_at == FIXED_NOW + timedelta(days=30)
    assert unknown is not None
    assert unknown.canada_presence is CanadaPresence.UNKNOWN
    assert unknown.next_scan_at is None

    deferred_next = deferred.next_scan_at
    again = harness.database.classify_canada_presence(
        now=FIXED_NOW + timedelta(hours=1),
        recheck_after=timedelta(days=30),
    )
    deferred_again = harness.database.get_source(paris.id)
    assert again == {"some": 1, "none": 1, "unknown": 1}
    assert deferred_again is not None
    assert deferred_again.next_scan_at == deferred_next


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


def test_removed_provider_cannot_be_added_stored_or_excluded(tmp_path: Path) -> None:
    database = Database(tmp_path / "removed-provider.sqlite3")
    try:
        with pytest.raises(ValueError, match="removed from this project"):
            database.upsert_source(
                SourceInput(
                    provider="smartrecruiters",
                    company="Acme",
                    board_identifier="acme",
                    career_page_url="https://jobs.smartrecruiters.com/acme",
                    support_status=SourceSupportStatus.SUPPORTED,
                ),
                now=FIXED_NOW,
            )
        with pytest.raises(ValueError, match="removed from this project"):
            database.add_source_exclusion(
                "smartrecruiters",
                "acme",
                "removed",
                now=FIXED_NOW,
            )

        inserted = database.bulk_upsert_sources(
            [
                SourceInput(
                    provider="smartrecruiters",
                    company="Acme",
                    board_identifier="acme",
                    career_page_url="https://jobs.smartrecruiters.com/acme",
                    support_status=SourceSupportStatus.SUPPORTED,
                ),
                SourceInput(
                    provider="greenhouse",
                    company="Example",
                    board_identifier="example",
                    career_page_url="https://boards.greenhouse.io/example",
                    support_status=SourceSupportStatus.SUPPORTED,
                ),
            ],
            now=FIXED_NOW,
        )

        assert inserted == 1
        assert [source.provider for source in database.list_sources()] == [
            "greenhouse"
        ]
        assert database.list_source_exclusions() == []
    finally:
        database.close()
