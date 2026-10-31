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
            event_object_type = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'job_events'"
            ).fetchone()[0]
    finally:
        upgraded.close()

    assert {
        "idx_jobs_repost_link",
        "idx_observations_repost_link",
    } <= names
    assert event_object_type == "view"


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


def test_opening_a_v5_database_adds_the_history_order_index(tmp_path: Path) -> None:
    path = tmp_path / "upgrade_v6.sqlite3"
    database = Database(path)
    try:
        with database.transaction() as connection:
            connection.execute("DROP INDEX IF EXISTS idx_jobs_last_seen")
            connection.execute(
                "UPDATE schema_metadata SET value = '5' WHERE key = 'schema_version'"
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
        assert "idx_jobs_last_seen" in names
    finally:
        upgraded.close()


def test_v6_event_table_migrates_to_a_non_storage_event_view(
    harness: EngineHarness,
    job_factory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("migration-job")],
        completed_at=FIXED_NOW,
    )
    with harness.database.transaction() as connection:
        connection.execute("DROP VIEW job_events")
        connection.execute(
            "CREATE TABLE job_events (id INTEGER PRIMARY KEY)"
        )
        connection.execute(
            "UPDATE schema_metadata SET value = '6' WHERE key = 'schema_version'"
        )
    harness.database.close()

    upgraded = Database(harness.path)
    try:
        assert upgraded.schema_version == SCHEMA_VERSION
        with upgraded.read() as connection:
            object_type = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'job_events'"
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT COUNT(*) FROM job_events"
            ).fetchone()[0]
            observation_count = connection.execute(
                "SELECT COUNT(*) FROM job_observations"
            ).fetchone()[0]
        assert object_type == "view"
        assert event_count == observation_count == 1
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


def test_due_source_ranking_prioritizes_confirmed_canada_presence(
    harness: EngineHarness,
) -> None:
    unknown = harness.database.upsert_source(
        SourceInput(
            provider="greenhouse",
            company="Unknown",
            board_identifier="unknown",
            career_page_url="https://boards.greenhouse.io/unknown",
            support_status=SourceSupportStatus.SUPPORTED,
        ),
        now=FIXED_NOW,
    )
    no_canada = harness.database.upsert_source(
        SourceInput(
            provider="greenhouse",
            company="No Canada",
            board_identifier="no-canada",
            career_page_url="https://boards.greenhouse.io/no-canada",
            support_status=SourceSupportStatus.SUPPORTED,
        ),
        now=FIXED_NOW,
    )
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE ats_sources SET canada_presence = 'SOME' WHERE id = ?",
            (harness.source.id,),
        )
        connection.execute(
            "UPDATE ats_sources SET canada_presence = 'NONE' WHERE id = ?",
            (no_canada.id,),
        )

    ranked = harness.database.due_sources_for_scan(
        FIXED_NOW,
        interval_seconds=3600,
    )

    assert [source.id for source in ranked] == [
        harness.source.id,
        unknown.id,
        no_canada.id,
    ]


def test_due_source_ranking_uses_limit_inside_sqlite(
    harness: EngineHarness,
) -> None:
    harness.database.upsert_source(
        SourceInput(
            provider="greenhouse",
            company="Later",
            board_identifier="later",
            career_page_url="https://boards.greenhouse.io/later",
            support_status=SourceSupportStatus.SUPPORTED,
        ),
        now=FIXED_NOW,
    )
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE ats_sources SET canada_presence = 'SOME' WHERE id = ?",
            (harness.source.id,),
        )

    ranked = harness.database.due_sources_for_scan(
        FIXED_NOW,
        interval_seconds=3600,
        limit=1,
    )

    assert [source.id for source in ranked] == [harness.source.id]


def test_source_reimport_preserves_operator_priority(tmp_path: Path) -> None:
    database = Database(tmp_path / "source-priority.sqlite3")
    source = SourceInput(
        provider="greenhouse",
        company="Priority Co",
        board_identifier="priority-co",
        career_page_url="https://boards.greenhouse.io/priority-co",
        support_status=SourceSupportStatus.SUPPORTED,
        priority=500,
    )
    replacement = SourceInput(
        provider=source.provider,
        company=source.company,
        board_identifier=source.board_identifier,
        career_page_url=source.career_page_url,
        support_status=source.support_status,
        priority=100,
    )
    try:
        database.bulk_upsert_sources([source], now=FIXED_NOW)
        database.bulk_upsert_sources([replacement], now=FIXED_NOW + timedelta(hours=1))

        persisted = database.get_source_by_board(
            source.provider, source.board_identifier
        )
        assert persisted is not None
        assert persisted.priority == 500
    finally:
        database.close()


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
