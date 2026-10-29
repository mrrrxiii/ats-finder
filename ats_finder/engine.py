"""Atomic complete-snapshot comparison and recent-job decision engine."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import hashlib
import json
import re
import sqlite3

from .db import Database, datetime_from_storage, datetime_to_storage
from .filters import classify_location, has_excluded_title, matches_java, plain_text
from .models import (
    ATSSource,
    ApplicationStatus,
    CanadaPresence,
    CoverageStats,
    DiscoveryType,
    EnginePolicy,
    JobRecord,
    JobState,
    LocationMatch,
    NormalizedJob,
    ObservationStatus,
    PublicationSemantics,
    RecentJob,
    ScanOutcome,
    ScanRun,
    ScanStatus,
    SourceInput,
    SourceLastScanResult,
    SourceSupportStatus,
    WorkplaceType,
    as_utc,
    utc_now,
)


_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)

# Character-level difflib ratios are quadratic, so a single 8 KB description pair
# costs over a second. Repost matching therefore compares the normalized title
# with SequenceMatcher (short strings) and descriptions with a bounded
# token-shingle Dice coefficient computed once per candidate.
_REPOST_TITLE_MAX_CHARS = 200
_REPOST_DESCRIPTION_MAX_TOKENS = 512
_REPOST_SHINGLE_TOKENS = 3
_REPOST_TITLE_TOKEN_OVERLAP = 0.5


@dataclass(frozen=True, slots=True)
class _RepostCandidate:
    """Pre-normalized job evidence reused across every repost comparison."""

    original_id: int
    fingerprint: str
    title: str
    title_tokens: frozenset[str]
    description_shingles: frozenset[int]
    location: str



class ScanEngine:
    """Persist scans and compare only consecutive successful complete snapshots."""

    def __init__(self, database: Database, policy: EnginePolicy | None = None) -> None:
        self.database = database
        self.policy = policy or EnginePolicy()

    def initialize(self) -> None:
        self.database.initialize()

    def upsert_source(
        self,
        source: SourceInput,
        *,
        now: datetime | None = None,
    ) -> ATSSource:
        return self.database.upsert_source(source, now=now)

    def get_source(self, source_id: int) -> ATSSource | None:
        return self.database.get_source(source_id)

    def list_sources(
        self,
        *,
        support_status: SourceSupportStatus | None = None,
        status: SourceSupportStatus | None = None,
        supported_only: bool = False,
        due_at: datetime | None = None,
        limit: int | None = None,
    ) -> list[ATSSource]:
        return self.database.list_sources(
            support_status=support_status,
            status=status,
            supported_only=supported_only,
            due_at=due_at,
            limit=limit,
        )

    def set_source_support(
        self,
        source_id: int,
        status: SourceSupportStatus,
        reason: str | None,
        *,
        now: datetime | None = None,
    ) -> ATSSource:
        return self.database.set_source_support(source_id, status, reason, now=now)

    def process_scan(
        self,
        source_id: int,
        jobs: Iterable[NormalizedJob] = (),
        *,
        status: ScanStatus = ScanStatus.SUCCESS,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        error: str | None = None,
        observed_record_count: int | None = None,
        next_scan_at: datetime | None = None,
    ) -> ScanOutcome:
        """Record one scan attempt.

        A ``SUCCESS`` is an explicit assertion by the caller that fetching,
        parsing, and all pagination completed. It atomically becomes the new
        baseline. Failed and incomplete attempts must not pass partial jobs and
        never alter job presence or the last successful baseline.
        """

        status = ScanStatus(status)
        completed = as_utc(completed_at) if completed_at is not None else utc_now()
        started = as_utc(started_at) if started_at is not None else completed
        scheduled = as_utc(next_scan_at) if next_scan_at is not None else None
        if started > completed:
            raise ValueError("started_at must not be later than completed_at")

        materialized = tuple(jobs)
        if any(not isinstance(job, NormalizedJob) for job in materialized):
            raise TypeError("jobs must contain only NormalizedJob instances")
        if status is not ScanStatus.SUCCESS:
            if materialized:
                raise ValueError("failed or incomplete scans cannot persist partial jobs")
            if not error or not error.strip():
                raise ValueError("failed or incomplete scans require error information")
            count = observed_record_count if observed_record_count is not None else 0
            if count < 0:
                raise ValueError("observed_record_count must be non-negative")
            return self._record_unsuccessful_scan(
                source_id,
                status,
                started,
                completed,
                error.strip(),
                count,
                scheduled,
            )

        if error is not None:
            raise ValueError("successful scans cannot contain error information")
        if observed_record_count is not None and observed_record_count != len(materialized):
            raise ValueError("successful scan record count must equal the number of jobs")
        identifiers = [job.provider_job_id for job in materialized]
        if len(set(identifiers)) != len(identifiers):
            duplicates = sorted(
                identifier
                for identifier, count in Counter(identifiers).items()
                if count > 1
            )
            raise ValueError(f"successful scan contains duplicate provider job ids: {duplicates}")
        return self._record_complete_scan(
            source_id,
            materialized,
            started,
            completed,
            scheduled,
        )

    def process_complete_scan(
        self,
        source_id: int,
        jobs: Iterable[NormalizedJob],
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        next_scan_at: datetime | None = None,
    ) -> ScanOutcome:
        return self.process_scan(
            source_id,
            jobs,
            status=ScanStatus.SUCCESS,
            started_at=started_at,
            completed_at=completed_at,
            next_scan_at=next_scan_at,
        )

    def record_failed_scan(
        self,
        source_id: int,
        error: str,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        observed_record_count: int = 0,
        next_scan_at: datetime | None = None,
    ) -> ScanOutcome:
        return self.process_scan(
            source_id,
            status=ScanStatus.FAILED,
            started_at=started_at,
            completed_at=completed_at,
            error=error,
            observed_record_count=observed_record_count,
            next_scan_at=next_scan_at,
        )

    def record_incomplete_scan(
        self,
        source_id: int,
        error: str,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        observed_record_count: int = 0,
        next_scan_at: datetime | None = None,
    ) -> ScanOutcome:
        return self.process_scan(
            source_id,
            status=ScanStatus.INCOMPLETE,
            started_at=started_at,
            completed_at=completed_at,
            error=error,
            observed_record_count=observed_record_count,
            next_scan_at=next_scan_at,
        )

    def set_application_status(
        self,
        job_id: int,
        status: ApplicationStatus,
        *,
        now: datetime | None = None,
    ) -> JobRecord:
        return self.database.set_application_status(
            job_id,
            status,
            allowed_statuses=self.policy.allowed_application_statuses,
            now=now,
        )

    def update_application_status(
        self,
        job_id: int,
        status: ApplicationStatus,
        *,
        now: datetime | None = None,
    ) -> JobRecord:
        return self.set_application_status(job_id, status, now=now)

    def list_job_history(
        self,
        *,
        job_id: int | None = None,
        source_id: int | None = None,
        event_type: ObservationStatus | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        return self.database.list_job_history(
            job_id=job_id,
            source_id=source_id,
            event_type=event_type,
            limit=limit,
        )

    def list_scan_runs(
        self,
        *,
        source_id: int | None = None,
        status: ScanStatus | None = None,
    ) -> list[ScanRun]:
        return self.database.list_scan_runs(source_id=source_id, status=status)

    def list_application_status_history(
        self,
        *,
        job_id: int | None = None,
        source_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        return self.database.list_application_status_history(
            job_id=job_id,
            source_id=source_id,
            limit=limit,
        )

    def coverage_stats(self, *, now: datetime | None = None) -> CoverageStats:
        return self.database.coverage_stats(
            now=now,
            stale_after=self.policy.stale_source_after,
        )

    def source_status_counts(self) -> dict[str, dict[str, int]]:
        return self.database.source_status_counts()

    def recent_jobs(
        self,
        *,
        now: datetime | None = None,
        window: timedelta | None = None,
        unified_sort: bool = False,
    ) -> list[RecentJob]:
        """Return confirmed Java/location matches after title exclusions.

        Verified publication and snapshot discovery remain visibly distinct.
        Potential reposts, uncertain locations, stale-snapshot discoveries, and
        removed jobs are excluded. A linked potential repost is included only
        when it independently has verified recent publication evidence.
        """

        current = as_utc(now) if now is not None else utc_now()
        effective_window = window or self.policy.recent_window
        if effective_window <= timedelta(0):
            raise ValueError("window must be positive")
        cutoff = current - effective_window
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT j.*, s.provider AS source_provider,
                       s.board_identifier AS source_board_identifier
                FROM jobs AS j
                JOIN ats_sources AS s ON s.id = j.source_id
                WHERE j.current_state = 'ACTIVE'
                  AND j.java_keyword_match = 1
                  AND j.location_match = 'CONFIRMED'
                """
            ).fetchall()

        results: list[RecentJob] = []
        for row in rows:
            if has_excluded_title(row["title"]):
                continue
            published_at = datetime_from_storage(row["published_at"])
            discovery_at = datetime_from_storage(row["discovery_at"])
            assert discovery_at is not None
            verified_recent = bool(row["publication_is_verified"]) and (
                published_at is not None and cutoff <= published_at <= current
            )
            snapshot_recent = (
                row["potential_repost_of_job_id"] is None
                and not bool(row["publication_is_verified"])
                and row["discovery_type"] == DiscoveryType.NEWLY_DETECTED.value
                and cutoff <= discovery_at <= current
                and row["discovery_snapshot_gap_seconds"] is not None
                and float(row["discovery_snapshot_gap_seconds"])
                <= self.policy.snapshot_max_gap.total_seconds()
            )
            if verified_recent:
                discovery_type = DiscoveryType.VERIFIED_RECENT
                displayed_publication = published_at
            elif snapshot_recent:
                discovery_type = DiscoveryType.NEWLY_DETECTED
                displayed_publication = None
            else:
                continue
            semantics = row["publication_semantics"]
            gap_seconds = row["discovery_snapshot_gap_seconds"]
            results.append(
                RecentJob(
                    job_id=int(row["id"]),
                    source_id=int(row["source_id"]),
                    company=row["company"],
                    title=row["title"],
                    location=row["location"],
                    workplace_type=WorkplaceType(row["workplace_type"]),
                    discovery_type=discovery_type,
                    published_at=displayed_publication,
                    publication_source=(
                        row["publication_source"] if verified_recent else None
                    ),
                    publication_semantics=(
                        PublicationSemantics(semantics)
                        if verified_recent and semantics is not None
                        else None
                    ),
                    first_seen_at=datetime_from_storage(row["first_seen_at"]),  # type: ignore[arg-type]
                    detection_at=discovery_at,
                    previous_board_scan_at=datetime_from_storage(
                        row["discovery_previous_scan_at"]
                    ),
                    snapshot_gap=(
                        timedelta(seconds=float(gap_seconds))
                        if gap_seconds is not None
                        else None
                    ),
                    provider=row["source_provider"],
                    board_identifier=row["source_board_identifier"],
                    application_url=row["application_url"],
                    application_status=ApplicationStatus(row["application_status"]),
                )
            )

        if unified_sort:
            results.sort(
                key=lambda item: (
                    item.published_at
                    if item.discovery_type is DiscoveryType.VERIFIED_RECENT
                    else item.detection_at,
                    item.job_id,
                ),
                reverse=True,
            )
        else:
            # Group trustworthy publication results first; order each group by
            # the timestamp whose semantics the UI will actually display.
            results.sort(
                key=lambda item: (
                    0 if item.discovery_type is DiscoveryType.VERIFIED_RECENT else 1,
                    -(
                        item.published_at
                        if item.discovery_type is DiscoveryType.VERIFIED_RECENT
                        else item.first_seen_at
                    ).timestamp(),
                    -item.job_id,
                )
            )
        return results

    def prune_expired_jobs(self, *, now: datetime | None = None) -> int:
        """Hard-delete jobs whose verified publication is older than retention.

        Deletion runs in bounded batches so a large backlog does not hold one
        write transaction (and its WAL) open for the whole purge.
        """

        current = as_utc(now) if now is not None else utc_now()
        cutoff = current - self.policy.verified_publication_retention
        batch_size = self.policy.retention_delete_batch_size
        deleted = 0
        while True:
            with self.database.transaction() as connection:
                batch = self._hard_delete_expired_jobs(
                    connection,
                    cutoff=cutoff,
                    limit=batch_size,
                )
            deleted += batch
            if batch < batch_size:
                return deleted

    def _record_unsuccessful_scan(
        self,
        source_id: int,
        status: ScanStatus,
        started: datetime,
        completed: datetime,
        error: str,
        record_count: int,
        next_scan_at: datetime | None,
    ) -> ScanOutcome:
        with self.database.transaction() as connection:
            self._require_source(connection, source_id)
            previous = self._previous_complete_scan(connection, source_id)
            previous_id, previous_at, gap_seconds = self._comparison_details(previous, completed)
            cursor = connection.execute(
                """
                INSERT INTO scan_runs (
                    source_id, started_at, ended_at, completion_status,
                    pagination_complete, record_count, error_information,
                    previous_complete_scan_id, previous_complete_scan_at,
                    snapshot_gap_seconds
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    datetime_to_storage(started),
                    datetime_to_storage(completed),
                    status.value,
                    record_count,
                    error,
                    previous_id,
                    datetime_to_storage(previous_at),
                    gap_seconds,
                ),
            )
            scan_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE ats_sources
                SET last_attempted_scan_at = ?, last_scan_result = ?,
                    consecutive_failures = consecutive_failures + 1,
                    next_scan_at = COALESCE(?, next_scan_at), updated_at = ?
                WHERE id = ?
                """,
                (
                    datetime_to_storage(completed),
                    SourceLastScanResult(status.value).value,
                    datetime_to_storage(next_scan_at),
                    datetime_to_storage(completed),
                    source_id,
                ),
            )
        return ScanOutcome(
            scan_id=scan_id,
            source_id=source_id,
            status=status,
            previous_complete_scan_id=previous_id,
            previous_complete_scan_at=previous_at,
            snapshot_gap=None if gap_seconds is None else timedelta(seconds=gap_seconds),
            record_count=record_count,
        )

    def _record_complete_scan(
        self,
        source_id: int,
        jobs: tuple[NormalizedJob, ...],
        started: datetime,
        completed: datetime,
        next_scan_at: datetime | None,
    ) -> ScanOutcome:
        raw_record_count = len(jobs)
        baseline_ids: list[str] = []
        added_ids: list[str] = []
        unchanged_ids: list[str] = []
        changed_ids: list[str] = []
        removed_ids: list[str] = []
        reappeared_ids: list[str] = []
        potential_repost_ids: list[str] = []
        canadian_jobs = 0

        with self.database.transaction() as connection:
            source = self._require_source(connection, source_id)
            cutoff = completed - self.policy.verified_publication_retention
            pre_retention_rows = connection.execute(
                "SELECT * FROM jobs WHERE source_id = ? ORDER BY id",
                (source_id,),
            ).fetchall()
            pre_retention_by_external_id = {
                row["provider_job_id"]: row for row in pre_retention_rows
            }
            retained_jobs: list[NormalizedJob] = []
            protected_job_ids: list[int] = []
            expired_existing_ids: list[int] = []
            for job in jobs:
                existing = pre_retention_by_external_id.get(job.provider_job_id)
                publication = self._resolve_publication_evidence(job, existing)
                if self._publication_is_expired(publication, cutoff):
                    if existing is not None:
                        expired_existing_ids.append(int(existing["id"]))
                    continue
                retained_jobs.append(job)
                if existing is not None:
                    protected_job_ids.append(int(existing["id"]))

            self._hard_delete_expired_jobs(
                connection,
                cutoff=cutoff,
                protected_job_ids=protected_job_ids,
                additional_job_ids=expired_existing_ids,
                limit=self.policy.retention_delete_batch_size,
            )
            jobs = tuple(retained_jobs)
            previous = self._previous_complete_scan(connection, source_id)
            previous_id, previous_at, gap_seconds = self._comparison_details(previous, completed)
            cursor = connection.execute(
                """
                INSERT INTO scan_runs (
                    source_id, started_at, ended_at, completion_status,
                    pagination_complete, record_count, error_information,
                    previous_complete_scan_id, previous_complete_scan_at,
                    snapshot_gap_seconds
                ) VALUES (?, ?, ?, 'SUCCESS', 1, ?, NULL, ?, ?, ?)
                """,
                (
                    source_id,
                    datetime_to_storage(started),
                    datetime_to_storage(completed),
                    raw_record_count,
                    previous_id,
                    datetime_to_storage(previous_at),
                    gap_seconds,
                ),
            )
            scan_id = int(cursor.lastrowid)

            previous_present_ids: set[str] = set()
            if previous_id is not None:
                previous_present_ids = {
                    row["provider_job_id"]
                    for row in connection.execute(
                        """
                        SELECT j.provider_job_id
                        FROM job_observations AS o
                        JOIN jobs AS j ON j.id = o.job_id
                        WHERE o.scan_id = ? AND o.is_present = 1
                        """,
                        (previous_id,),
                    )
                }

            history_rows = connection.execute(
                "SELECT * FROM jobs WHERE source_id = ? ORDER BY id",
                (source_id,),
            ).fetchall()
            history_by_external_id = {row["provider_job_id"]: row for row in history_rows}
            repost_candidates: dict[str, list[_RepostCandidate]] = {}
            for row in history_rows:
                self._add_repost_candidate(repost_candidates, row)
            current_ids = {job.provider_job_id for job in jobs}

            for job in sorted(jobs, key=lambda value: value.provider_job_id):
                existing = history_by_external_id.get(job.provider_job_id)
                company = job.company or source["company"]
                decision = classify_location(
                    job.location,
                    job.description,
                    job.workplace_type,
                    job.canada_remote_eligibility,
                )
                if decision.match in (
                    LocationMatch.CONFIRMED,
                    LocationMatch.REVIEW_LOCATION,
                ):
                    canadian_jobs += 1
                java_match = matches_java(job.title, job.description)
                evidence = self._resolve_publication_evidence(job, existing)
                fingerprint = self._content_fingerprint(
                    company=company,
                    job=job,
                    workplace_type=decision.workplace_type,
                    canada_remote_eligibility=decision.canada_remote_eligibility,
                    publication=evidence,
                )
                verified_classification = self._verified_classification(evidence, completed)

                potential_repost_of: int | None = None
                repost_similarity: float | None = None
                if existing is None:
                    match = self._find_potential_repost(
                        job,
                        company,
                        fingerprint,
                        repost_candidates,
                    )
                    if match is not None:
                        potential_repost_of, repost_similarity = match

                if existing is None:
                    if potential_repost_of is not None:
                        observation_status = ObservationStatus.POTENTIAL_REPOST
                        discovery_type = (
                            DiscoveryType.VERIFIED_RECENT
                            if verified_classification is DiscoveryType.VERIFIED_RECENT
                            else DiscoveryType.POTENTIAL_REPOST
                        )
                        potential_repost_ids.append(job.provider_job_id)
                    elif verified_classification is not None:
                        observation_status = (
                            ObservationStatus.BASELINE
                            if previous_id is None
                            else ObservationStatus.ADDED
                        )
                        discovery_type = verified_classification
                    elif previous_id is None:
                        observation_status = ObservationStatus.BASELINE
                        discovery_type = DiscoveryType.BASELINE
                    elif gap_seconds is not None and gap_seconds <= self.policy.snapshot_max_gap.total_seconds():
                        observation_status = ObservationStatus.ADDED
                        discovery_type = DiscoveryType.NEWLY_DETECTED
                    else:
                        observation_status = ObservationStatus.ADDED
                        discovery_type = DiscoveryType.NEWLY_DETECTED_TIME_UNCERTAIN

                    if previous_id is None and observation_status is ObservationStatus.BASELINE:
                        baseline_ids.append(job.provider_job_id)
                    elif observation_status is not ObservationStatus.POTENTIAL_REPOST:
                        added_ids.append(job.provider_job_id)

                    composite_identity = json.dumps(
                        [source["provider"], source["board_identifier"], job.provider_job_id],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    job_id = self._insert_job(
                        connection,
                        source_id=source_id,
                        composite_identity=composite_identity,
                        provider_job_id=job.provider_job_id,
                        company=company,
                        job=job,
                        decision=decision,
                        java_match=java_match,
                        publication=evidence,
                        timestamp=completed,
                        fingerprint=fingerprint,
                        discovery_type=discovery_type,
                        previous_scan_at=previous_at,
                        snapshot_gap_seconds=gap_seconds,
                        potential_repost_of=potential_repost_of,
                        repost_similarity=repost_similarity,
                    )
                    existing = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                    assert existing is not None
                    history_by_external_id[job.provider_job_id] = existing
                    self._add_repost_candidate(repost_candidates, existing)
                else:
                    job_id = int(existing["id"])
                    was_in_previous_snapshot = job.provider_job_id in previous_present_ids
                    if was_in_previous_snapshot:
                        observation_status = (
                            ObservationStatus.UNCHANGED
                            if existing["content_fingerprint"] == fingerprint
                            else ObservationStatus.CHANGED
                        )
                        if observation_status is ObservationStatus.UNCHANGED:
                            unchanged_ids.append(job.provider_job_id)
                        else:
                            changed_ids.append(job.provider_job_id)
                        discovery_type = DiscoveryType(existing["discovery_type"])
                        discovery_at = datetime_from_storage(existing["discovery_at"])
                        discovery_previous_scan_at = datetime_from_storage(
                            existing["discovery_previous_scan_at"]
                        )
                        discovery_gap = existing["discovery_snapshot_gap_seconds"]
                        potential_repost_of = existing["potential_repost_of_job_id"]
                        repost_similarity = existing["repost_similarity"]
                        if (
                            potential_repost_of is not None
                            and verified_classification is not DiscoveryType.VERIFIED_RECENT
                        ):
                            discovery_type = DiscoveryType.POTENTIAL_REPOST
                        elif verified_classification is not None:
                            # A subsequently exposed trustworthy old timestamp
                            # invalidates snapshot-new status rather than being
                            # ignored to keep a job in recent results.
                            discovery_type = verified_classification
                            if DiscoveryType(existing["discovery_type"]) is not discovery_type:
                                discovery_at = completed
                    else:
                        observation_status = ObservationStatus.REAPPEARED
                        reappeared_ids.append(job.provider_job_id)
                        discovery_type = (
                            DiscoveryType.POTENTIAL_REPOST
                            if (
                                existing["potential_repost_of_job_id"] is not None
                                and verified_classification is not DiscoveryType.VERIFIED_RECENT
                            )
                            else verified_classification or DiscoveryType.REAPPEARED
                        )
                        discovery_at = completed
                        discovery_previous_scan_at = previous_at
                        discovery_gap = gap_seconds
                        potential_repost_of = existing["potential_repost_of_job_id"]
                        repost_similarity = existing["repost_similarity"]

                    self._update_job(
                        connection,
                        job_id=job_id,
                        company=company,
                        job=job,
                        decision=decision,
                        java_match=java_match,
                        publication=evidence,
                        timestamp=completed,
                        fingerprint=fingerprint,
                        discovery_type=discovery_type,
                        discovery_at=discovery_at,
                        previous_scan_at=discovery_previous_scan_at,
                        snapshot_gap_seconds=discovery_gap,
                        potential_repost_of=potential_repost_of,
                        repost_similarity=repost_similarity,
                    )

                self._insert_observation(
                    connection,
                    scan_id=scan_id,
                    job_id=job_id,
                    timestamp=completed,
                    observation_status=observation_status,
                    is_present=True,
                    fingerprint=fingerprint,
                    discovery_type=discovery_type,
                    comparison_scan_id=previous_id,
                    comparison_scan_at=previous_at,
                    snapshot_gap_seconds=gap_seconds,
                    company=company,
                    job=job,
                    workplace_type=decision.workplace_type,
                    canada_remote_eligibility=decision.canada_remote_eligibility,
                    location_match=decision.match,
                    location_reason=decision.reason,
                    java_match=java_match,
                    publication=evidence,
                    potential_repost_of=potential_repost_of,
                    repost_similarity=repost_similarity,
                )

            for removed_external_id in sorted(previous_present_ids - current_ids):
                removed = history_by_external_id[removed_external_id]
                removed_ids.append(removed_external_id)
                connection.execute(
                    """
                    UPDATE jobs
                    SET current_state = 'REMOVED', removed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        datetime_to_storage(completed),
                        datetime_to_storage(completed),
                        removed["id"],
                    ),
                )
                self._insert_removed_observation(
                    connection,
                    scan_id=scan_id,
                    previous_scan_id=previous_id,
                    previous_scan_at=previous_at,
                    gap_seconds=gap_seconds,
                    timestamp=completed,
                    row=removed,
                )

            if not jobs:
                presence = CanadaPresence.UNKNOWN
            elif canadian_jobs:
                presence = CanadaPresence.SOME
            else:
                presence = CanadaPresence.NONE
            effective_next_scan = next_scan_at
            if presence is CanadaPresence.NONE:
                effective_next_scan = completed + self.policy.no_canada_recheck

            connection.execute(
                """
                UPDATE ats_sources
                SET last_successful_scan_at = ?, last_attempted_scan_at = ?,
                    last_scan_result = 'SUCCESS', consecutive_failures = 0,
                    canada_presence = ?, next_scan_at = COALESCE(?, next_scan_at),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    datetime_to_storage(completed),
                    datetime_to_storage(completed),
                    presence.value,
                    datetime_to_storage(effective_next_scan),
                    datetime_to_storage(completed),
                    source_id,
                ),
            )

        return ScanOutcome(
            scan_id=scan_id,
            source_id=source_id,
            status=ScanStatus.SUCCESS,
            previous_complete_scan_id=previous_id,
            previous_complete_scan_at=previous_at,
            snapshot_gap=None if gap_seconds is None else timedelta(seconds=gap_seconds),
            record_count=raw_record_count,
            baseline=tuple(baseline_ids),
            added=tuple(added_ids),
            unchanged=tuple(unchanged_ids),
            changed=tuple(changed_ids),
            removed=tuple(removed_ids),
            reappeared=tuple(reappeared_ids),
            potential_reposts=tuple(potential_repost_ids),
        )

    @staticmethod
    def _publication_is_expired(
        publication: dict[str, object | None],
        cutoff: datetime,
    ) -> bool:
        if not bool(publication["publication_is_verified"]):
            return False
        published_at = publication["published_at"]
        assert isinstance(published_at, datetime)
        return published_at < cutoff

    @staticmethod
    def _hard_delete_expired_jobs(
        connection: sqlite3.Connection,
        *,
        cutoff: datetime,
        protected_job_ids: Iterable[int] = (),
        additional_job_ids: Iterable[int] = (),
        limit: int | None = None,
    ) -> int:
        """Stage retention targets, clear repost links, and cascade-delete them.

        ``limit`` bounds how many expired jobs a single call removes. Scans pass
        a small batch so a large retention backlog drains over several runs
        instead of blocking the event loop for minutes inside one snapshot.
        """

        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive when provided")
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS _ats_finder_retention_protected (
                job_id INTEGER PRIMARY KEY
            )
            """
        )
        connection.execute("DELETE FROM _ats_finder_retention_protected")
        connection.executemany(
            "INSERT OR IGNORE INTO _ats_finder_retention_protected(job_id) VALUES (?)",
            ((int(job_id),) for job_id in protected_job_ids),
        )
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS _ats_finder_retention_targets (
                job_id INTEGER PRIMARY KEY
            )
            """
        )
        connection.execute("DELETE FROM _ats_finder_retention_targets")
        staging = """
            INSERT OR IGNORE INTO _ats_finder_retention_targets(job_id)
            SELECT id
            FROM jobs
            WHERE publication_is_verified = 1 AND published_at < ?
              AND id NOT IN (SELECT job_id FROM _ats_finder_retention_protected)
            """
        parameters: tuple[object, ...] = (datetime_to_storage(cutoff),)
        if limit is not None:
            staging += " LIMIT ?"
            parameters += (int(limit),)
        connection.execute(staging, parameters)
        connection.execute("DELETE FROM _ats_finder_retention_protected")
        connection.executemany(
            "INSERT OR IGNORE INTO _ats_finder_retention_targets(job_id) VALUES (?)",
            ((int(job_id),) for job_id in additional_job_ids),
        )
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM _ats_finder_retention_targets"
        ).fetchone()
        target_count = int(row["count"])
        if target_count:
            for table in ("jobs", "job_observations", "job_events"):
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET potential_repost_of_job_id = NULL, repost_similarity = NULL
                    WHERE potential_repost_of_job_id IN (
                        SELECT job_id FROM _ats_finder_retention_targets
                    )
                    """
                )
            connection.execute(
                """
                DELETE FROM jobs
                WHERE id IN (SELECT job_id FROM _ats_finder_retention_targets)
                """
            )
        connection.execute("DELETE FROM _ats_finder_retention_targets")
        return target_count

    @staticmethod
    def _require_source(connection: sqlite3.Connection, source_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM ats_sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown source id {source_id}")
        return row

    @staticmethod
    def _previous_complete_scan(
        connection: sqlite3.Connection,
        source_id: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM scan_runs
            WHERE source_id = ? AND completion_status = 'SUCCESS'
              AND pagination_complete = 1
            ORDER BY ended_at DESC, id DESC
            LIMIT 1
            """,
            (source_id,),
        ).fetchone()

    @staticmethod
    def _comparison_details(
        previous: sqlite3.Row | None,
        completed: datetime,
    ) -> tuple[int | None, datetime | None, float | None]:
        if previous is None:
            return None, None, None
        previous_at = datetime_from_storage(previous["ended_at"])
        assert previous_at is not None
        gap = (completed - previous_at).total_seconds()
        if gap < 0:
            raise ValueError("completed_at predates the previous successful scan")
        return int(previous["id"]), previous_at, gap

    @staticmethod
    def _resolve_publication_evidence(
        job: NormalizedJob,
        existing: sqlite3.Row | None,
    ) -> dict[str, object | None]:
        if job.published_at is not None:
            incoming = {
                "published_at": job.published_at,
                "publication_source": job.publication_source,
                "publication_semantics": (
                    job.publication_semantics.value
                    if job.publication_semantics is not None
                    else None
                ),
                "publication_is_verified": job.publication_is_verified,
            }
            # Never replace already verified publication evidence with weaker,
            # undocumented metadata from a later response.
            if (
                existing is not None
                and bool(existing["publication_is_verified"])
                and not job.publication_is_verified
            ):
                return {
                    "published_at": datetime_from_storage(existing["published_at"]),
                    "publication_source": existing["publication_source"],
                    "publication_semantics": existing["publication_semantics"],
                    "publication_is_verified": True,
                }
            return incoming
        if existing is not None and existing["published_at"] is not None:
            return {
                "published_at": datetime_from_storage(existing["published_at"]),
                "publication_source": existing["publication_source"],
                "publication_semantics": existing["publication_semantics"],
                "publication_is_verified": bool(existing["publication_is_verified"]),
            }
        return {
            "published_at": None,
            "publication_source": None,
            "publication_semantics": None,
            "publication_is_verified": False,
        }

    def _verified_classification(
        self,
        publication: dict[str, object | None],
        observed_at: datetime,
    ) -> DiscoveryType | None:
        if not publication["publication_is_verified"]:
            return None
        published_at = publication["published_at"]
        assert isinstance(published_at, datetime)
        if published_at > observed_at:
            return DiscoveryType.VERIFIED_TIME_INVALID
        if observed_at - self.policy.recent_window <= published_at <= observed_at:
            return DiscoveryType.VERIFIED_RECENT
        return DiscoveryType.VERIFIED_OLD

    @staticmethod
    def _content_fingerprint(
        *,
        company: str,
        job: NormalizedJob,
        workplace_type: WorkplaceType,
        canada_remote_eligibility: bool | None,
        publication: dict[str, object | None],
    ) -> str:
        published_at = publication["published_at"]
        payload = {
            "company": plain_text(company),
            "title": plain_text(job.title),
            "description": plain_text(job.description),
            "location": plain_text(job.location),
            "workplace_type": workplace_type.value,
            "canada_remote_eligibility": canada_remote_eligibility,
            "published_at": (
                datetime_to_storage(published_at)
                if isinstance(published_at, datetime)
                else None
            ),
            "publication_source": publication["publication_source"],
            "publication_semantics": publication["publication_semantics"],
            "publication_is_verified": publication["publication_is_verified"],
            "application_url": job.application_url.strip(),
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _similarity_text(value: str) -> str:
        return _NON_WORD_RE.sub(" ", plain_text(value).casefold()).strip()

    @staticmethod
    def _title_tokens(title: str) -> frozenset[str]:
        return frozenset(title.split())

    @classmethod
    def _description_shingles(cls, description: str) -> frozenset[int]:
        tokens = description.split()
        if len(tokens) > _REPOST_DESCRIPTION_MAX_TOKENS:
            tokens = tokens[:_REPOST_DESCRIPTION_MAX_TOKENS]
        if len(tokens) < _REPOST_SHINGLE_TOKENS:
            return frozenset(hash(token) for token in tokens)
        return frozenset(
            hash(" ".join(tokens[index : index + _REPOST_SHINGLE_TOKENS]))
            for index in range(len(tokens) - _REPOST_SHINGLE_TOKENS + 1)
        )

    @staticmethod
    def _dice_similarity(left: frozenset[int], right: frozenset[int]) -> float:
        if not left or not right:
            return 0.0
        return (2.0 * len(left & right)) / (len(left) + len(right))

    @staticmethod
    def _title_similarity(left: str, right: str) -> float:
        return SequenceMatcher(
            None,
            left[:_REPOST_TITLE_MAX_CHARS],
            right[:_REPOST_TITLE_MAX_CHARS],
            autojunk=False,
        ).ratio()

    @staticmethod
    def _token_overlap(left: frozenset[str], right: frozenset[str]) -> float:
        if not left or not right:
            return 0.0
        union = len(left | right)
        return len(left & right) / union if union else 0.0

    def _repost_candidate(self, row: sqlite3.Row) -> tuple[str, _RepostCandidate]:
        company = self._similarity_text(row["company"] or "")
        title = self._similarity_text(row["title"] or "")
        return company, _RepostCandidate(
            original_id=int(row["potential_repost_of_job_id"] or row["id"]),
            fingerprint=str(row["content_fingerprint"]),
            title=title,
            title_tokens=self._title_tokens(title),
            description_shingles=self._description_shingles(
                self._similarity_text(row["description"] or "")
            ),
            location=self._similarity_text(row["location"] or ""),
        )

    def _add_repost_candidate(
        self,
        candidates: dict[str, list[_RepostCandidate]],
        row: sqlite3.Row,
    ) -> None:
        company, candidate = self._repost_candidate(row)
        candidates.setdefault(company, []).append(candidate)

    def _find_potential_repost(
        self,
        job: NormalizedJob,
        company: str,
        fingerprint: str,
        candidates: dict[str, list[_RepostCandidate]],
    ) -> tuple[int, float] | None:
        title = self._similarity_text(job.title)
        title_tokens = self._title_tokens(title)
        description_shingles = self._description_shingles(
            self._similarity_text(job.description)
        )
        location = self._similarity_text(job.location)
        normalized_company = self._similarity_text(company)
        best: tuple[int, float] | None = None
        for candidate in candidates.get(normalized_company, ()):
            if candidate.fingerprint == fingerprint:
                score = 1.0
            else:
                if (
                    self._token_overlap(title_tokens, candidate.title_tokens)
                    < _REPOST_TITLE_TOKEN_OVERLAP
                ):
                    continue
                title_score = self._title_similarity(title, candidate.title)
                if title_score < self.policy.repost_title_similarity_threshold:
                    continue
                location_score = self._title_similarity(location, candidate.location)
                if description_shingles and candidate.description_shingles:
                    description_score = self._dice_similarity(
                        description_shingles, candidate.description_shingles
                    )
                    score = 0.65 * title_score + 0.25 * description_score + 0.10 * location_score
                else:
                    score = 0.85 * title_score + 0.15 * location_score
            if score >= self.policy.repost_similarity_threshold and (
                best is None or score > best[1]
            ):
                best = (candidate.original_id, score)
        return best

    @staticmethod
    def _insert_job(
        connection: sqlite3.Connection,
        *,
        source_id: int,
        composite_identity: str,
        provider_job_id: str,
        company: str,
        job: NormalizedJob,
        decision,
        java_match: bool,
        publication: dict[str, object | None],
        timestamp: datetime,
        fingerprint: str,
        discovery_type: DiscoveryType,
        previous_scan_at: datetime | None,
        snapshot_gap_seconds: float | None,
        potential_repost_of: int | None,
        repost_similarity: float | None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO jobs (
                source_id, composite_identity, provider_job_id, company, title,
                description, location, workplace_type, canada_remote_eligibility,
                location_match, location_reason, java_keyword_match,
                published_at, publication_source, publication_semantics,
                publication_is_verified, first_seen_at, last_seen_at,
                current_state, removed_at, application_url, application_status,
                content_fingerprint, discovery_type, discovery_at,
                discovery_previous_scan_at, discovery_snapshot_gap_seconds,
                potential_repost_of_job_id, repost_similarity, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'ACTIVE', NULL, ?, 'NOT_APPLIED', ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                source_id,
                composite_identity,
                provider_job_id,
                company,
                job.title,
                job.description,
                job.location,
                decision.workplace_type.value,
                decision.canada_remote_eligibility,
                decision.match.value,
                decision.reason,
                int(java_match),
                datetime_to_storage(publication["published_at"]),  # type: ignore[arg-type]
                publication["publication_source"],
                publication["publication_semantics"],
                int(bool(publication["publication_is_verified"])),
                datetime_to_storage(timestamp),
                datetime_to_storage(timestamp),
                job.application_url,
                fingerprint,
                discovery_type.value,
                datetime_to_storage(timestamp),
                datetime_to_storage(previous_scan_at),
                snapshot_gap_seconds,
                potential_repost_of,
                repost_similarity,
                datetime_to_storage(timestamp),
                datetime_to_storage(timestamp),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _update_job(
        connection: sqlite3.Connection,
        *,
        job_id: int,
        company: str,
        job: NormalizedJob,
        decision,
        java_match: bool,
        publication: dict[str, object | None],
        timestamp: datetime,
        fingerprint: str,
        discovery_type: DiscoveryType,
        discovery_at: datetime,
        previous_scan_at: datetime | None,
        snapshot_gap_seconds: float | None,
        potential_repost_of: int | None,
        repost_similarity: float | None,
    ) -> None:
        connection.execute(
            """
            UPDATE jobs SET
                company = ?, title = ?, description = ?, location = ?,
                workplace_type = ?, canada_remote_eligibility = ?,
                location_match = ?, location_reason = ?, java_keyword_match = ?,
                published_at = ?, publication_source = ?, publication_semantics = ?,
                publication_is_verified = ?, last_seen_at = ?, current_state = 'ACTIVE',
                removed_at = NULL, application_url = ?, content_fingerprint = ?,
                discovery_type = ?, discovery_at = ?, discovery_previous_scan_at = ?,
                discovery_snapshot_gap_seconds = ?, potential_repost_of_job_id = ?,
                repost_similarity = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                company,
                job.title,
                job.description,
                job.location,
                decision.workplace_type.value,
                decision.canada_remote_eligibility,
                decision.match.value,
                decision.reason,
                int(java_match),
                datetime_to_storage(publication["published_at"]),  # type: ignore[arg-type]
                publication["publication_source"],
                publication["publication_semantics"],
                int(bool(publication["publication_is_verified"])),
                datetime_to_storage(timestamp),
                job.application_url,
                fingerprint,
                discovery_type.value,
                datetime_to_storage(discovery_at),
                datetime_to_storage(previous_scan_at),
                snapshot_gap_seconds,
                potential_repost_of,
                repost_similarity,
                datetime_to_storage(timestamp),
                job_id,
            ),
        )

    @staticmethod
    def _insert_observation(
        connection: sqlite3.Connection,
        *,
        scan_id: int,
        job_id: int,
        timestamp: datetime,
        observation_status: ObservationStatus,
        is_present: bool,
        fingerprint: str,
        discovery_type: DiscoveryType,
        comparison_scan_id: int | None,
        comparison_scan_at: datetime | None,
        snapshot_gap_seconds: float | None,
        company: str,
        job: NormalizedJob,
        workplace_type: WorkplaceType,
        canada_remote_eligibility: bool | None,
        location_match: LocationMatch,
        location_reason: str,
        java_match: bool,
        publication: dict[str, object | None],
        potential_repost_of: int | None,
        repost_similarity: float | None,
    ) -> None:
        del company  # company is immutable in jobs and not required by the observation table
        connection.execute(
            """
            INSERT INTO job_observations (
                scan_id, job_id, observation_timestamp, observation_status,
                is_present, content_fingerprint, discovery_type,
                comparison_scan_id, comparison_scan_at, snapshot_gap_seconds,
                title, description, location, workplace_type,
                canada_remote_eligibility, location_match, location_reason,
                java_keyword_match, published_at, publication_source,
                publication_semantics, publication_is_verified, application_url,
                potential_repost_of_job_id, repost_similarity
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                scan_id,
                job_id,
                datetime_to_storage(timestamp),
                observation_status.value,
                int(is_present),
                fingerprint,
                discovery_type.value,
                comparison_scan_id,
                datetime_to_storage(comparison_scan_at),
                snapshot_gap_seconds,
                job.title,
                job.description,
                job.location,
                workplace_type.value,
                canada_remote_eligibility,
                location_match.value,
                location_reason,
                int(java_match),
                datetime_to_storage(publication["published_at"]),  # type: ignore[arg-type]
                publication["publication_source"],
                publication["publication_semantics"],
                int(bool(publication["publication_is_verified"])),
                job.application_url,
                potential_repost_of,
                repost_similarity,
            ),
        )

    @staticmethod
    def _insert_removed_observation(
        connection: sqlite3.Connection,
        *,
        scan_id: int,
        previous_scan_id: int | None,
        previous_scan_at: datetime | None,
        gap_seconds: float | None,
        timestamp: datetime,
        row: sqlite3.Row,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_observations (
                scan_id, job_id, observation_timestamp, observation_status,
                is_present, content_fingerprint, discovery_type,
                comparison_scan_id, comparison_scan_at, snapshot_gap_seconds,
                title, description, location, workplace_type,
                canada_remote_eligibility, location_match, location_reason,
                java_keyword_match, published_at, publication_source,
                publication_semantics, publication_is_verified, application_url,
                potential_repost_of_job_id, repost_similarity
            ) VALUES (
                ?, ?, ?, 'REMOVED', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                scan_id,
                row["id"],
                datetime_to_storage(timestamp),
                row["content_fingerprint"],
                row["discovery_type"],
                previous_scan_id,
                datetime_to_storage(previous_scan_at),
                gap_seconds,
                row["title"],
                row["description"],
                row["location"],
                row["workplace_type"],
                row["canada_remote_eligibility"],
                row["location_match"],
                row["location_reason"],
                row["java_keyword_match"],
                row["published_at"],
                row["publication_source"],
                row["publication_semantics"],
                row["publication_is_verified"],
                row["application_url"],
                row["potential_repost_of_job_id"],
                row["repost_similarity"],
            ),
        )

