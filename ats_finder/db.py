"""SQLite persistence for sources, scans, jobs, and immutable observations."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import threading

from .models import (
    ATSSource,
    ApplicationStatus,
    CoverageStats,
    DiscoveryType,
    JobObservation,
    JobRecord,
    JobState,
    LocationMatch,
    ObservationStatus,
    PublicationSemantics,
    ScanRun,
    ScanStatus,
    SourceInput,
    SourceLastScanResult,
    SourceSupportStatus,
    WorkplaceType,
    as_utc,
    utc_now,
)


SCHEMA_VERSION = 2


def datetime_to_storage(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = as_utc(value)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def datetime_from_storage(value: str | None) -> datetime | None:
    if value is None:
        return None
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _optional_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


class Database:
    """Thread-safe SQLite store.

    A single connection is retained so ``:memory:`` databases work in tests.
    Every write API uses ``BEGIN IMMEDIATE`` and the scan engine performs each
    complete snapshot in one transaction.
    """

    def __init__(
        self,
        path: str | Path = "ats_finder.sqlite3",
        *,
        timeout_seconds: float = 30.0,
        initialize: bool = True,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
        if self.path != ":memory:":
            journal_mode = self._connection.execute("PRAGMA journal_mode").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        with self._lock:
            # Opening an already-current database is a read-only operation.
            # This path is used by every Streamlit rerun while the worker may
            # hold SQLite's single writer slot for an atomic board checkpoint.
            try:
                current = self._connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table: schema_metadata" not in str(exc).casefold():
                    raise
                current = None
            if current is not None:
                current_version = int(current["value"])
                if current_version == SCHEMA_VERSION:
                    return
                if current_version != 1:
                    raise RuntimeError(
                        "unsupported database schema version "
                        f"{current_version}; expected 1 or {SCHEMA_VERSION}"
                    )

            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ats_sources (
                    id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL COLLATE NOCASE,
                    company TEXT NOT NULL,
                    board_identifier TEXT NOT NULL,
                    career_page_url TEXT NOT NULL,
                    support_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    support_reason TEXT,
                    last_successful_scan_at TEXT,
                    last_attempted_scan_at TEXT,
                    last_scan_result TEXT NOT NULL DEFAULT 'NOT_SCANNED',
                    next_scan_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
                    priority INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 0),
                    source_url TEXT,
                    dataset_name TEXT,
                    dataset_revision TEXT,
                    dataset_record_id TEXT,
                    imported_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (provider, board_identifier)
                );

                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES ats_sources(id) ON DELETE CASCADE,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    completion_status TEXT NOT NULL,
                    pagination_complete INTEGER NOT NULL CHECK (pagination_complete IN (0, 1)),
                    record_count INTEGER NOT NULL CHECK (record_count >= 0),
                    error_information TEXT,
                    previous_complete_scan_id INTEGER REFERENCES scan_runs(id),
                    previous_complete_scan_at TEXT,
                    snapshot_gap_seconds REAL CHECK (snapshot_gap_seconds IS NULL OR snapshot_gap_seconds >= 0)
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES ats_sources(id) ON DELETE CASCADE,
                    composite_identity TEXT NOT NULL UNIQUE,
                    provider_job_id TEXT NOT NULL,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    location TEXT NOT NULL,
                    workplace_type TEXT NOT NULL,
                    canada_remote_eligibility INTEGER CHECK (canada_remote_eligibility IN (0, 1) OR canada_remote_eligibility IS NULL),
                    location_match TEXT NOT NULL,
                    location_reason TEXT NOT NULL,
                    java_keyword_match INTEGER NOT NULL CHECK (java_keyword_match IN (0, 1)),
                    published_at TEXT,
                    publication_source TEXT,
                    publication_semantics TEXT,
                    publication_is_verified INTEGER NOT NULL DEFAULT 0 CHECK (publication_is_verified IN (0, 1)),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    removed_at TEXT,
                    application_url TEXT NOT NULL,
                    application_status TEXT NOT NULL DEFAULT 'NOT_APPLIED',
                    content_fingerprint TEXT NOT NULL,
                    discovery_type TEXT NOT NULL,
                    discovery_at TEXT NOT NULL,
                    discovery_previous_scan_at TEXT,
                    discovery_snapshot_gap_seconds REAL CHECK (
                        discovery_snapshot_gap_seconds IS NULL OR discovery_snapshot_gap_seconds >= 0
                    ),
                    potential_repost_of_job_id INTEGER REFERENCES jobs(id),
                    repost_similarity REAL CHECK (
                        repost_similarity IS NULL OR (repost_similarity >= 0 AND repost_similarity <= 1)
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (source_id, provider_job_id)
                );

                CREATE TABLE IF NOT EXISTS job_observations (
                    id INTEGER PRIMARY KEY,
                    scan_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    observation_timestamp TEXT NOT NULL,
                    observation_status TEXT NOT NULL,
                    is_present INTEGER NOT NULL CHECK (is_present IN (0, 1)),
                    content_fingerprint TEXT NOT NULL,
                    discovery_type TEXT NOT NULL,
                    comparison_scan_id INTEGER REFERENCES scan_runs(id),
                    comparison_scan_at TEXT,
                    snapshot_gap_seconds REAL CHECK (snapshot_gap_seconds IS NULL OR snapshot_gap_seconds >= 0),
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    location TEXT NOT NULL,
                    workplace_type TEXT NOT NULL,
                    canada_remote_eligibility INTEGER CHECK (canada_remote_eligibility IN (0, 1) OR canada_remote_eligibility IS NULL),
                    location_match TEXT NOT NULL,
                    location_reason TEXT NOT NULL,
                    java_keyword_match INTEGER NOT NULL CHECK (java_keyword_match IN (0, 1)),
                    published_at TEXT,
                    publication_source TEXT,
                    publication_semantics TEXT,
                    publication_is_verified INTEGER NOT NULL CHECK (publication_is_verified IN (0, 1)),
                    application_url TEXT NOT NULL,
                    potential_repost_of_job_id INTEGER REFERENCES jobs(id),
                    repost_similarity REAL CHECK (
                        repost_similarity IS NULL OR (repost_similarity >= 0 AND repost_similarity <= 1)
                    ),
                    UNIQUE (scan_id, job_id)
                );

                CREATE TABLE IF NOT EXISTS job_versions (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    first_observation_id INTEGER NOT NULL UNIQUE
                        REFERENCES job_observations(id) ON DELETE CASCADE,
                    first_scan_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                    recorded_at TEXT NOT NULL,
                    content_fingerprint TEXT NOT NULL,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    location TEXT NOT NULL,
                    workplace_type TEXT NOT NULL,
                    canada_remote_eligibility INTEGER CHECK (
                        canada_remote_eligibility IN (0, 1)
                        OR canada_remote_eligibility IS NULL
                    ),
                    location_match TEXT NOT NULL,
                    location_reason TEXT NOT NULL,
                    java_keyword_match INTEGER NOT NULL CHECK (java_keyword_match IN (0, 1)),
                    published_at TEXT,
                    publication_source TEXT,
                    publication_semantics TEXT,
                    publication_is_verified INTEGER NOT NULL CHECK (
                        publication_is_verified IN (0, 1)
                    ),
                    application_url TEXT NOT NULL,
                    UNIQUE (job_id, content_fingerprint)
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY,
                    observation_id INTEGER NOT NULL UNIQUE
                        REFERENCES job_observations(id) ON DELETE CASCADE,
                    scan_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    event_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    is_present INTEGER NOT NULL CHECK (is_present IN (0, 1)),
                    content_fingerprint TEXT NOT NULL,
                    discovery_type TEXT NOT NULL,
                    comparison_scan_id INTEGER REFERENCES scan_runs(id),
                    comparison_scan_at TEXT,
                    snapshot_gap_seconds REAL CHECK (
                        snapshot_gap_seconds IS NULL OR snapshot_gap_seconds >= 0
                    ),
                    potential_repost_of_job_id INTEGER REFERENCES jobs(id),
                    repost_similarity REAL CHECK (
                        repost_similarity IS NULL
                        OR (repost_similarity >= 0 AND repost_similarity <= 1)
                    )
                );

                CREATE TABLE IF NOT EXISTS application_status_history (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    previous_status TEXT,
                    new_status TEXT NOT NULL,
                    changed_at TEXT,
                    recorded_at TEXT NOT NULL,
                    change_kind TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sources_schedule
                    ON ats_sources(next_scan_at, priority, id);
                CREATE INDEX IF NOT EXISTS idx_scans_source_status_end
                    ON scan_runs(source_id, completion_status, ended_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_source_state
                    ON jobs(source_id, current_state);
                CREATE INDEX IF NOT EXISTS idx_jobs_recent_publication
                    ON jobs(publication_is_verified, published_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_recent_discovery
                    ON jobs(discovery_type, discovery_at);
                CREATE INDEX IF NOT EXISTS idx_observations_scan_present
                    ON job_observations(scan_id, is_present);
                CREATE INDEX IF NOT EXISTS idx_observations_job_time
                    ON job_observations(job_id, observation_timestamp DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_job_versions_job_time
                    ON job_versions(job_id, recorded_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_job_events_job_time
                    ON job_events(job_id, event_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_job_events_type_time
                    ON job_events(event_type, event_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_application_history_job_time
                    ON application_status_history(job_id, recorded_at DESC, id DESC);

                CREATE TRIGGER IF NOT EXISTS trg_observation_job_version
                AFTER INSERT ON job_observations
                WHEN NEW.is_present = 1
                BEGIN
                    INSERT OR IGNORE INTO job_versions (
                        job_id, first_observation_id, first_scan_id, recorded_at,
                        content_fingerprint, company, title, description, location,
                        workplace_type, canada_remote_eligibility, location_match,
                        location_reason, java_keyword_match, published_at,
                        publication_source, publication_semantics,
                        publication_is_verified, application_url
                    ) VALUES (
                        NEW.job_id, NEW.id, NEW.scan_id, NEW.observation_timestamp,
                        NEW.content_fingerprint,
                        (SELECT company FROM jobs WHERE id = NEW.job_id),
                        NEW.title, NEW.description, NEW.location, NEW.workplace_type,
                        NEW.canada_remote_eligibility, NEW.location_match,
                        NEW.location_reason, NEW.java_keyword_match, NEW.published_at,
                        NEW.publication_source, NEW.publication_semantics,
                        NEW.publication_is_verified, NEW.application_url
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_observation_job_event
                AFTER INSERT ON job_observations
                BEGIN
                    INSERT INTO job_events (
                        observation_id, scan_id, job_id, event_at, event_type,
                        is_present, content_fingerprint, discovery_type,
                        comparison_scan_id, comparison_scan_at,
                        snapshot_gap_seconds, potential_repost_of_job_id,
                        repost_similarity
                    ) VALUES (
                        NEW.id, NEW.scan_id, NEW.job_id,
                        NEW.observation_timestamp, NEW.observation_status,
                        NEW.is_present, NEW.content_fingerprint, NEW.discovery_type,
                        NEW.comparison_scan_id, NEW.comparison_scan_at,
                        NEW.snapshot_gap_seconds, NEW.potential_repost_of_job_id,
                        NEW.repost_similarity
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_job_initial_application_status
                AFTER INSERT ON jobs
                BEGIN
                    INSERT INTO application_status_history (
                        job_id, previous_status, new_status, changed_at,
                        recorded_at, change_kind
                    ) VALUES (
                        NEW.id, NULL, NEW.application_status, NEW.created_at,
                        NEW.created_at, 'JOB_CREATED'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_job_application_status_change
                AFTER UPDATE OF application_status ON jobs
                WHEN OLD.application_status <> NEW.application_status
                BEGIN
                    INSERT INTO application_status_history (
                        job_id, previous_status, new_status, changed_at,
                        recorded_at, change_kind
                    ) VALUES (
                        NEW.id, OLD.application_status, NEW.application_status,
                        NEW.updated_at, NEW.updated_at, 'USER_UPDATE'
                    );
                END;
                """
            )
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()
                existing_version = 1 if row is None else int(row["value"])
                if existing_version not in {1, SCHEMA_VERSION}:
                    raise RuntimeError(
                        "unsupported database schema version "
                        f"{existing_version}; expected 1 or {SCHEMA_VERSION}"
                    )
                if existing_version == 1:
                    self._backfill_audit_tables()
                self._connection.execute(
                    """
                    INSERT INTO schema_metadata(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(SCHEMA_VERSION),),
                )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _backfill_audit_tables(self) -> None:
        """Populate v2 audit records without inventing historical change times."""

        self._connection.execute(
            """
            INSERT OR IGNORE INTO job_versions (
                job_id, first_observation_id, first_scan_id, recorded_at,
                content_fingerprint, company, title, description, location,
                workplace_type, canada_remote_eligibility, location_match,
                location_reason, java_keyword_match, published_at,
                publication_source, publication_semantics,
                publication_is_verified, application_url
            )
            SELECT
                o.job_id, o.id, o.scan_id, o.observation_timestamp,
                o.content_fingerprint, j.company, o.title, o.description,
                o.location, o.workplace_type, o.canada_remote_eligibility,
                o.location_match, o.location_reason, o.java_keyword_match,
                o.published_at, o.publication_source, o.publication_semantics,
                o.publication_is_verified, o.application_url
            FROM job_observations AS o
            JOIN jobs AS j ON j.id = o.job_id
            WHERE o.is_present = 1
            ORDER BY o.id
            """
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO job_events (
                observation_id, scan_id, job_id, event_at, event_type,
                is_present, content_fingerprint, discovery_type,
                comparison_scan_id, comparison_scan_at, snapshot_gap_seconds,
                potential_repost_of_job_id, repost_similarity
            )
            SELECT
                id, scan_id, job_id, observation_timestamp,
                observation_status, is_present, content_fingerprint,
                discovery_type, comparison_scan_id, comparison_scan_at,
                snapshot_gap_seconds, potential_repost_of_job_id,
                repost_similarity
            FROM job_observations
            ORDER BY id
            """
        )
        recorded_at = datetime_to_storage(utc_now())
        self._connection.execute(
            """
            INSERT INTO application_status_history (
                job_id, previous_status, new_status, changed_at,
                recorded_at, change_kind
            )
            SELECT j.id, NULL, j.application_status, NULL, ?, 'MIGRATION_BASELINE'
            FROM jobs AS j
            WHERE NOT EXISTS (
                SELECT 1
                FROM application_status_history AS h
                WHERE h.job_id = j.id
            )
            """,
            (recorded_at,),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open an atomic immediate transaction on the shared connection."""

        with self._lock:
            if self._connection.in_transaction:
                raise RuntimeError("nested database transactions are not supported")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        with self.read() as connection:
            row = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is None:
            raise RuntimeError("database is not initialized")
        return int(row["value"])

    def upsert_source(self, source: SourceInput, *, now: datetime | None = None) -> ATSSource:
        timestamp = as_utc(now) if now is not None else utc_now()
        imported_at = source.imported_at
        if imported_at is None and any(
            value is not None
            for value in (
                source.source_url,
                source.dataset_name,
                source.dataset_revision,
                source.dataset_record_id,
            )
        ):
            imported_at = timestamp
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO ats_sources (
                    provider, company, board_identifier, career_page_url,
                    support_status, support_reason, next_scan_at, priority,
                    source_url, dataset_name, dataset_revision, dataset_record_id,
                    imported_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, board_identifier) DO UPDATE SET
                    company = excluded.company,
                    career_page_url = excluded.career_page_url,
                    support_status = excluded.support_status,
                    support_reason = excluded.support_reason,
                    next_scan_at = COALESCE(excluded.next_scan_at, ats_sources.next_scan_at),
                    priority = excluded.priority,
                    source_url = COALESCE(excluded.source_url, ats_sources.source_url),
                    dataset_name = COALESCE(excluded.dataset_name, ats_sources.dataset_name),
                    dataset_revision = COALESCE(excluded.dataset_revision, ats_sources.dataset_revision),
                    dataset_record_id = COALESCE(excluded.dataset_record_id, ats_sources.dataset_record_id),
                    imported_at = COALESCE(ats_sources.imported_at, excluded.imported_at),
                    updated_at = excluded.updated_at
                """,
                (
                    source.provider,
                    source.company,
                    source.board_identifier,
                    source.career_page_url,
                    source.support_status.value,
                    source.support_reason,
                    datetime_to_storage(source.next_scan_at),
                    source.priority,
                    source.source_url,
                    source.dataset_name,
                    source.dataset_revision,
                    source.dataset_record_id,
                    datetime_to_storage(imported_at),
                    datetime_to_storage(timestamp),
                    datetime_to_storage(timestamp),
                ),
            )
            row = connection.execute(
                "SELECT * FROM ats_sources WHERE provider = ? AND board_identifier = ?",
                (source.provider, source.board_identifier),
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("source upsert did not return a row")
        return self._source_from_row(row)

    def bulk_upsert_sources(
        self,
        sources: Collection[SourceInput],
        *,
        now: datetime | None = None,
    ) -> int:
        """Upsert a dataset batch in one transaction and return its row count."""

        timestamp = as_utc(now) if now is not None else utc_now()
        timestamp_value = datetime_to_storage(timestamp)
        parameters: list[tuple[object, ...]] = []
        for source in sources:
            if not isinstance(source, SourceInput):
                raise TypeError("sources must contain only SourceInput instances")
            imported_at = source.imported_at
            if imported_at is None and any(
                value is not None
                for value in (
                    source.source_url,
                    source.dataset_name,
                    source.dataset_revision,
                    source.dataset_record_id,
                )
            ):
                imported_at = timestamp
            parameters.append(
                (
                    source.provider,
                    source.company,
                    source.board_identifier,
                    source.career_page_url,
                    source.support_status.value,
                    source.support_reason,
                    datetime_to_storage(source.next_scan_at),
                    source.priority,
                    source.source_url,
                    source.dataset_name,
                    source.dataset_revision,
                    source.dataset_record_id,
                    datetime_to_storage(imported_at),
                    timestamp_value,
                    timestamp_value,
                )
            )
        if not parameters:
            return 0
        with self.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO ats_sources (
                    provider, company, board_identifier, career_page_url,
                    support_status, support_reason, next_scan_at, priority,
                    source_url, dataset_name, dataset_revision, dataset_record_id,
                    imported_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, board_identifier) DO UPDATE SET
                    company = excluded.company,
                    career_page_url = excluded.career_page_url,
                    support_status = excluded.support_status,
                    support_reason = excluded.support_reason,
                    next_scan_at = COALESCE(excluded.next_scan_at, ats_sources.next_scan_at),
                    priority = excluded.priority,
                    source_url = COALESCE(excluded.source_url, ats_sources.source_url),
                    dataset_name = COALESCE(excluded.dataset_name, ats_sources.dataset_name),
                    dataset_revision = COALESCE(excluded.dataset_revision, ats_sources.dataset_revision),
                    dataset_record_id = COALESCE(excluded.dataset_record_id, ats_sources.dataset_record_id),
                    imported_at = COALESCE(ats_sources.imported_at, excluded.imported_at),
                    updated_at = excluded.updated_at
                """,
                parameters,
            )
        return len(parameters)

    def set_source_support(
        self,
        source_id: int,
        status: SourceSupportStatus,
        reason: str | None,
        *,
        now: datetime | None = None,
    ) -> ATSSource:
        status = SourceSupportStatus(status)
        timestamp = as_utc(now) if now is not None else utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE ats_sources
                SET support_status = ?, support_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, reason, datetime_to_storage(timestamp), source_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown source id {source_id}")
            row = connection.execute("SELECT * FROM ats_sources WHERE id = ?", (source_id,)).fetchone()
        assert row is not None
        return self._source_from_row(row)

    def update_source_schedule(
        self,
        source_id: int,
        *,
        next_scan_at: datetime | None,
        priority: int | None = None,
        now: datetime | None = None,
    ) -> ATSSource:
        if priority is not None and priority < 0:
            raise ValueError("priority must be non-negative")
        timestamp = as_utc(now) if now is not None else utc_now()
        scheduled = as_utc(next_scan_at) if next_scan_at is not None else None
        with self.transaction() as connection:
            if priority is None:
                cursor = connection.execute(
                    "UPDATE ats_sources SET next_scan_at = ?, updated_at = ? WHERE id = ?",
                    (datetime_to_storage(scheduled), datetime_to_storage(timestamp), source_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE ats_sources
                    SET next_scan_at = ?, priority = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        datetime_to_storage(scheduled),
                        priority,
                        datetime_to_storage(timestamp),
                        source_id,
                    ),
                )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown source id {source_id}")
            row = connection.execute("SELECT * FROM ats_sources WHERE id = ?", (source_id,)).fetchone()
        assert row is not None
        return self._source_from_row(row)

    def get_source(self, source_id: int) -> ATSSource | None:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM ats_sources WHERE id = ?", (source_id,)).fetchone()
        return None if row is None else self._source_from_row(row)

    def get_source_by_board(self, provider: str, board_identifier: str) -> ATSSource | None:
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM ats_sources WHERE provider = ? AND board_identifier = ?",
                (provider, board_identifier),
            ).fetchone()
        return None if row is None else self._source_from_row(row)

    def list_sources(
        self,
        *,
        support_status: SourceSupportStatus | None = None,
        status: SourceSupportStatus | None = None,
        supported_only: bool = False,
        due_at: datetime | None = None,
        limit: int | None = None,
    ) -> list[ATSSource]:
        if support_status is not None and status is not None:
            if SourceSupportStatus(support_status) is not SourceSupportStatus(status):
                raise ValueError("support_status and status disagree")
        selected_status = support_status if support_status is not None else status
        if supported_only:
            if (
                selected_status is not None
                and SourceSupportStatus(selected_status) is not SourceSupportStatus.SUPPORTED
            ):
                raise ValueError("supported_only conflicts with the requested status")
            selected_status = SourceSupportStatus.SUPPORTED
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")

        clauses: list[str] = []
        parameters: list[object] = []
        if selected_status is not None:
            clauses.append("support_status = ?")
            parameters.append(SourceSupportStatus(selected_status).value)
        if due_at is not None:
            due_at = as_utc(due_at)
            clauses.append("(next_scan_at IS NULL OR next_scan_at <= ?)")
            parameters.append(datetime_to_storage(due_at))
        query = "SELECT * FROM ats_sources"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        if due_at is not None:
            query += (
                " ORDER BY priority DESC, "
                "COALESCE(last_attempted_scan_at, '0001-01-01T00:00:00Z') ASC, id ASC"
            )
        else:
            query += " ORDER BY priority DESC, company COLLATE NOCASE, id"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self.read() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._source_from_row(row) for row in rows]

    def due_sources(self, now: datetime, *, limit: int | None = None) -> list[ATSSource]:
        return self.list_sources(
            supported_only=True,
            due_at=as_utc(now),
            limit=limit,
        )

    def get_job(self, job_id: int) -> JobRecord | None:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else self._job_from_row(row)

    def get_job_by_external_id(self, source_id: int, provider_job_id: str) -> JobRecord | None:
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE source_id = ? AND provider_job_id = ?",
                (source_id, provider_job_id),
            ).fetchone()
        return None if row is None else self._job_from_row(row)

    def list_jobs(
        self,
        *,
        source_id: int | None = None,
        state: JobState | None = None,
    ) -> list[JobRecord]:
        clauses: list[str] = []
        parameters: list[object] = []
        if source_id is not None:
            clauses.append("source_id = ?")
            parameters.append(source_id)
        if state is not None:
            clauses.append("current_state = ?")
            parameters.append(JobState(state).value)
        query = "SELECT * FROM jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY first_seen_at DESC, id DESC"
        with self.read() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._job_from_row(row) for row in rows]

    def set_application_status(
        self,
        job_id: int,
        status: ApplicationStatus,
        *,
        allowed_statuses: Collection[ApplicationStatus] | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        status = ApplicationStatus(status)
        if allowed_statuses is not None:
            allowed = {ApplicationStatus(item) for item in allowed_statuses}
            if status not in allowed:
                raise ValueError(f"application status {status.value} is disabled by policy")
        timestamp = as_utc(now) if now is not None else utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"unknown job id {job_id}")
            if existing["application_status"] == status.value:
                return self._job_from_row(existing)
            cursor = connection.execute(
                "UPDATE jobs SET application_status = ?, updated_at = ? WHERE id = ?",
                (status.value, datetime_to_storage(timestamp), job_id),
            )
            if cursor.rowcount != 1:  # pragma: no cover - selected in this transaction
                raise RuntimeError(f"application status update lost job id {job_id}")
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row is not None
        return self._job_from_row(row)

    def update_application_status(
        self,
        job_id: int,
        status: ApplicationStatus,
        *,
        allowed_statuses: Collection[ApplicationStatus] | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        """Compatibility name for :meth:`set_application_status`."""

        return self.set_application_status(
            job_id,
            status,
            allowed_statuses=allowed_statuses,
            now=now,
        )

    def get_scan_run(self, scan_id: int) -> ScanRun | None:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM scan_runs WHERE id = ?", (scan_id,)).fetchone()
        return None if row is None else self._scan_from_row(row)

    def list_scan_runs(
        self,
        *,
        source_id: int | None = None,
        status: ScanStatus | None = None,
    ) -> list[ScanRun]:
        clauses: list[str] = []
        parameters: list[object] = []
        if source_id is not None:
            clauses.append("source_id = ?")
            parameters.append(source_id)
        if status is not None:
            clauses.append("completion_status = ?")
            parameters.append(ScanStatus(status).value)
        query = "SELECT * FROM scan_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ended_at DESC, id DESC"
        with self.read() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._scan_from_row(row) for row in rows]

    def list_observations(
        self,
        *,
        scan_id: int | None = None,
        job_id: int | None = None,
    ) -> list[JobObservation]:
        if scan_id is None and job_id is None:
            raise ValueError("scan_id or job_id is required")
        clauses: list[str] = []
        parameters: list[object] = []
        if scan_id is not None:
            clauses.append("scan_id = ?")
            parameters.append(scan_id)
        if job_id is not None:
            clauses.append("job_id = ?")
            parameters.append(job_id)
        query = (
            "SELECT * FROM job_observations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY observation_timestamp, id"
        )
        with self.read() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._observation_from_row(row) for row in rows]

    def list_job_versions(
        self,
        *,
        job_id: int | None = None,
        source_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        """Return immutable content versions, newest first.

        Audit-query methods intentionally return dictionaries so callers can
        export them directly without depending on a second set of domain
        models. Timestamps use the same canonical UTC storage representation as
        SQLite (ISO 8601 ending in ``Z``).
        """

        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[object] = []
        if job_id is not None:
            clauses.append("v.job_id = ?")
            parameters.append(job_id)
        if source_id is not None:
            clauses.append("j.source_id = ?")
            parameters.append(source_id)
        query = """
            SELECT v.*, j.source_id, j.provider_job_id,
                   s.provider, s.board_identifier
            FROM job_versions AS v
            JOIN jobs AS j ON j.id = v.job_id
            JOIN ats_sources AS s ON s.id = j.source_id
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY v.recorded_at DESC, v.id DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self.read() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [dict(row) for row in rows]

    def list_job_events(
        self,
        *,
        job_id: int | None = None,
        source_id: int | None = None,
        event_type: ObservationStatus | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        """Return immutable scan-derived job events, newest first."""

        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[object] = []
        if job_id is not None:
            clauses.append("e.job_id = ?")
            parameters.append(job_id)
        if source_id is not None:
            clauses.append("j.source_id = ?")
            parameters.append(source_id)
        if event_type is not None:
            clauses.append("e.event_type = ?")
            parameters.append(ObservationStatus(event_type).value)
        query = """
            SELECT e.*, j.source_id, j.provider_job_id, j.company,
                   j.application_status, o.title, o.description, o.location,
                   o.workplace_type, o.location_match, o.location_reason,
                   o.java_keyword_match, o.published_at,
                   o.publication_source, o.publication_semantics,
                   o.publication_is_verified, o.application_url,
                   s.provider, s.board_identifier
            FROM job_events AS e
            JOIN jobs AS j ON j.id = e.job_id
            JOIN job_observations AS o ON o.id = e.observation_id
            JOIN ats_sources AS s ON s.id = j.source_id
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY e.event_at DESC, e.id DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self.read() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [dict(row) for row in rows]

    def list_job_history(
        self,
        *,
        job_id: int | None = None,
        source_id: int | None = None,
        event_type: ObservationStatus | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        """Dashboard-friendly alias for the immutable job event stream."""

        return self.list_job_events(
            job_id=job_id,
            source_id=source_id,
            event_type=event_type,
            limit=limit,
        )

    def list_application_status_history(
        self,
        *,
        job_id: int | None = None,
        source_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        """Return application-status audit records, newest first."""

        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[object] = []
        if job_id is not None:
            clauses.append("h.job_id = ?")
            parameters.append(job_id)
        if source_id is not None:
            clauses.append("j.source_id = ?")
            parameters.append(source_id)
        query = """
            SELECT h.*, j.source_id, j.provider_job_id, j.company, j.title,
                   s.provider, s.board_identifier
            FROM application_status_history AS h
            JOIN jobs AS j ON j.id = h.job_id
            JOIN ats_sources AS s ON s.id = j.source_id
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY h.recorded_at DESC, h.id DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self.read() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [dict(row) for row in rows]

    def coverage_stats(
        self,
        *,
        now: datetime | None = None,
        stale_after: timedelta | None = None,
    ) -> CoverageStats:
        if stale_after is not None and stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        now = as_utc(now) if now is not None else utc_now()
        with self.read() as connection:
            source_counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS imported,
                    SUM(CASE WHEN last_successful_scan_at IS NOT NULL THEN 1 ELSE 0 END) AS successful,
                    SUM(CASE WHEN last_scan_result = 'FAILED' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN last_scan_result = 'INCOMPLETE' THEN 1 ELSE 0 END) AS incomplete,
                    SUM(CASE WHEN last_scan_result = 'NOT_SCANNED' THEN 1 ELSE 0 END) AS not_scanned
                FROM ats_sources
                """
            ).fetchone()
            job_counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN current_state = 'ACTIVE' THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN publication_is_verified = 1 THEN 1 ELSE 0 END) AS verified,
                    (
                        SELECT COUNT(DISTINCT job_id)
                        FROM job_events
                        WHERE event_type = 'ADDED'
                          AND discovery_type = 'NEWLY_DETECTED'
                    ) AS newly_detected,
                    SUM(CASE WHEN location_match = 'REVIEW_LOCATION' THEN 1 ELSE 0 END) AS ambiguous,
                    SUM(CASE WHEN discovery_type = 'NEWLY_DETECTED_TIME_UNCERTAIN' THEN 1 ELSE 0 END) AS uncertain
                FROM jobs
                """
            ).fetchone()
            stale = None
            if stale_after is not None:
                cutoff = now - stale_after
                stale_row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM ats_sources
                    WHERE last_successful_scan_at IS NOT NULL
                      AND last_successful_scan_at < ?
                    """,
                    (datetime_to_storage(cutoff),),
                ).fetchone()
                stale = int(stale_row["count"])
        assert source_counts is not None and job_counts is not None
        return CoverageStats(
            imported_sources=int(source_counts["imported"] or 0),
            successfully_scanned_sources=int(source_counts["successful"] or 0),
            failed_sources=int(source_counts["failed"] or 0),
            incomplete_sources=int(source_counts["incomplete"] or 0),
            not_yet_scanned_sources=int(source_counts["not_scanned"] or 0),
            stale_sources=stale,
            total_jobs_inspected=int(job_counts["total"] or 0),
            active_jobs=int(job_counts["active"] or 0),
            jobs_with_verified_publication_timestamps=int(job_counts["verified"] or 0),
            newly_detected_jobs=int(job_counts["newly_detected"] or 0),
            ambiguous_location_jobs=int(job_counts["ambiguous"] or 0),
            insufficient_history_jobs=int(job_counts["uncertain"] or 0),
        )

    def source_status_counts(self) -> dict[str, dict[str, int]]:
        """Return support and latest-scan counts as separate dimensions."""

        support = {item.value: 0 for item in SourceSupportStatus}
        scan = {item.value: 0 for item in SourceLastScanResult}
        with self.read() as connection:
            for row in connection.execute(
                "SELECT support_status, COUNT(*) AS count FROM ats_sources GROUP BY support_status"
            ):
                support[row["support_status"]] = int(row["count"])
            for row in connection.execute(
                "SELECT last_scan_result, COUNT(*) AS count FROM ats_sources GROUP BY last_scan_result"
            ):
                scan[row["last_scan_result"]] = int(row["count"])
        return {"support_status": support, "last_scan_result": scan}

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> ATSSource:
        return ATSSource(
            id=int(row["id"]),
            provider=row["provider"],
            company=row["company"],
            board_identifier=row["board_identifier"],
            career_page_url=row["career_page_url"],
            support_status=SourceSupportStatus(row["support_status"]),
            support_reason=row["support_reason"],
            last_successful_scan_at=datetime_from_storage(row["last_successful_scan_at"]),
            last_attempted_scan_at=datetime_from_storage(row["last_attempted_scan_at"]),
            last_scan_result=SourceLastScanResult(row["last_scan_result"]),
            next_scan_at=datetime_from_storage(row["next_scan_at"]),
            consecutive_failures=int(row["consecutive_failures"]),
            priority=int(row["priority"]),
            source_url=row["source_url"],
            dataset_name=row["dataset_name"],
            dataset_revision=row["dataset_revision"],
            dataset_record_id=row["dataset_record_id"],
            imported_at=datetime_from_storage(row["imported_at"]),
            created_at=datetime_from_storage(row["created_at"]),  # type: ignore[arg-type]
            updated_at=datetime_from_storage(row["updated_at"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        semantics = row["publication_semantics"]
        return JobRecord(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            composite_identity=row["composite_identity"],
            provider_job_id=row["provider_job_id"],
            company=row["company"],
            title=row["title"],
            description=row["description"],
            location=row["location"],
            workplace_type=WorkplaceType(row["workplace_type"]),
            canada_remote_eligibility=_optional_bool(row["canada_remote_eligibility"]),
            location_match=LocationMatch(row["location_match"]),
            location_reason=row["location_reason"],
            java_keyword_match=bool(row["java_keyword_match"]),
            published_at=datetime_from_storage(row["published_at"]),
            publication_source=row["publication_source"],
            publication_semantics=None if semantics is None else PublicationSemantics(semantics),
            publication_is_verified=bool(row["publication_is_verified"]),
            first_seen_at=datetime_from_storage(row["first_seen_at"]),  # type: ignore[arg-type]
            last_seen_at=datetime_from_storage(row["last_seen_at"]),  # type: ignore[arg-type]
            current_state=JobState(row["current_state"]),
            removed_at=datetime_from_storage(row["removed_at"]),
            application_url=row["application_url"],
            application_status=ApplicationStatus(row["application_status"]),
            content_fingerprint=row["content_fingerprint"],
            discovery_type=DiscoveryType(row["discovery_type"]),
            discovery_at=datetime_from_storage(row["discovery_at"]),  # type: ignore[arg-type]
            discovery_previous_scan_at=datetime_from_storage(row["discovery_previous_scan_at"]),
            discovery_snapshot_gap_seconds=row["discovery_snapshot_gap_seconds"],
            potential_repost_of_job_id=row["potential_repost_of_job_id"],
            repost_similarity=row["repost_similarity"],
            created_at=datetime_from_storage(row["created_at"]),  # type: ignore[arg-type]
            updated_at=datetime_from_storage(row["updated_at"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _scan_from_row(row: sqlite3.Row) -> ScanRun:
        return ScanRun(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            started_at=datetime_from_storage(row["started_at"]),  # type: ignore[arg-type]
            ended_at=datetime_from_storage(row["ended_at"]),  # type: ignore[arg-type]
            completion_status=ScanStatus(row["completion_status"]),
            pagination_complete=bool(row["pagination_complete"]),
            record_count=int(row["record_count"]),
            error_information=row["error_information"],
            previous_complete_scan_id=row["previous_complete_scan_id"],
            previous_complete_scan_at=datetime_from_storage(row["previous_complete_scan_at"]),
            snapshot_gap_seconds=row["snapshot_gap_seconds"],
        )

    @staticmethod
    def _observation_from_row(row: sqlite3.Row) -> JobObservation:
        semantics = row["publication_semantics"]
        return JobObservation(
            id=int(row["id"]),
            scan_id=int(row["scan_id"]),
            job_id=int(row["job_id"]),
            observation_timestamp=datetime_from_storage(row["observation_timestamp"]),  # type: ignore[arg-type]
            observation_status=ObservationStatus(row["observation_status"]),
            is_present=bool(row["is_present"]),
            content_fingerprint=row["content_fingerprint"],
            discovery_type=DiscoveryType(row["discovery_type"]),
            comparison_scan_id=row["comparison_scan_id"],
            comparison_scan_at=datetime_from_storage(row["comparison_scan_at"]),
            snapshot_gap_seconds=row["snapshot_gap_seconds"],
            title=row["title"],
            description=row["description"],
            location=row["location"],
            workplace_type=WorkplaceType(row["workplace_type"]),
            canada_remote_eligibility=_optional_bool(row["canada_remote_eligibility"]),
            location_match=LocationMatch(row["location_match"]),
            location_reason=row["location_reason"],
            java_keyword_match=bool(row["java_keyword_match"]),
            published_at=datetime_from_storage(row["published_at"]),
            publication_source=row["publication_source"],
            publication_semantics=None if semantics is None else PublicationSemantics(semantics),
            publication_is_verified=bool(row["publication_is_verified"]),
            application_url=row["application_url"],
            potential_repost_of_job_id=row["potential_repost_of_job_id"],
            repost_similarity=row["repost_similarity"],
        )

