"""Typed domain models for ATS source monitoring.

The models deliberately keep publication evidence separate from observation time.
An ATS adapter may supply a publication timestamp only when it can also describe
where that timestamp came from and what it means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Final


UTC: Final = timezone.utc


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Validate an aware timestamp and normalize it to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class ScanStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"


class SourceSupportStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"


class SourceLastScanResult(StrEnum):
    NOT_SCANNED = "NOT_SCANNED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"


class WorkplaceType(StrEnum):
    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ONSITE = "ONSITE"
    UNKNOWN = "UNKNOWN"


class LocationMatch(StrEnum):
    CONFIRMED = "CONFIRMED"
    REVIEW_LOCATION = "REVIEW_LOCATION"
    REJECTED = "REJECTED"


class JobState(StrEnum):
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class ObservationStatus(StrEnum):
    BASELINE = "BASELINE"
    ADDED = "ADDED"
    UNCHANGED = "UNCHANGED"
    CHANGED = "CHANGED"
    REMOVED = "REMOVED"
    REAPPEARED = "REAPPEARED"
    POTENTIAL_REPOST = "POTENTIAL_REPOST"


class DiscoveryType(StrEnum):
    BASELINE = "BASELINE"
    VERIFIED_RECENT = "VERIFIED_RECENT"
    VERIFIED_OLD = "VERIFIED_OLD"
    VERIFIED_TIME_INVALID = "VERIFIED_TIME_INVALID"
    NEWLY_DETECTED = "NEWLY_DETECTED"
    NEWLY_DETECTED_TIME_UNCERTAIN = "NEWLY_DETECTED_TIME_UNCERTAIN"
    REAPPEARED = "REAPPEARED"
    POTENTIAL_REPOST = "POTENTIAL_REPOST"


class PublicationSemantics(StrEnum):
    """Supported meanings for a provider's documented timestamp.

    There is intentionally no ``updated_at`` member: update timestamps are not
    publication evidence. ``UNKNOWN`` can be stored for inspection but cannot be
    marked verified.
    """

    ORIGINAL_PUBLICATION = "original_publication"
    REPUBLICATION = "republication"
    ATS_PUBLISHED_AT = "ats_documented_published_at"
    SCHEMA_ORG_DATE_POSTED = "schema_org_date_posted"
    OFFICIAL_FEED_DATE_POSTED = "official_feed_date_posted"
    UNKNOWN = "unknown"


class ApplicationStatus(StrEnum):
    NOT_APPLIED = "NOT_APPLIED"
    SAVED = "SAVED"
    APPLIED = "APPLIED"
    INTERVIEWING = "INTERVIEWING"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class SourceInput:
    provider: str
    company: str
    board_identifier: str
    career_page_url: str
    support_status: SourceSupportStatus = SourceSupportStatus.UNKNOWN
    support_reason: str | None = None
    next_scan_at: datetime | None = None
    priority: int = 100
    source_url: str | None = None
    dataset_name: str | None = None
    dataset_revision: str | None = None
    dataset_record_id: str | None = None
    imported_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("provider", "company", "board_identifier", "career_page_url"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must not be blank")
            object.__setattr__(self, name, value.strip())
        if self.priority < 0:
            raise ValueError("priority must be non-negative")
        object.__setattr__(self, "support_status", SourceSupportStatus(self.support_status))
        if self.next_scan_at is not None:
            object.__setattr__(self, "next_scan_at", as_utc(self.next_scan_at))
        if self.imported_at is not None:
            object.__setattr__(self, "imported_at", as_utc(self.imported_at))


@dataclass(frozen=True, slots=True)
class ATSSource:
    id: int
    provider: str
    company: str
    board_identifier: str
    career_page_url: str
    support_status: SourceSupportStatus
    support_reason: str | None
    last_successful_scan_at: datetime | None
    last_attempted_scan_at: datetime | None
    last_scan_result: SourceLastScanResult
    next_scan_at: datetime | None
    consecutive_failures: int
    priority: int
    source_url: str | None
    dataset_name: str | None
    dataset_revision: str | None
    dataset_record_id: str | None
    imported_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NormalizedJob:
    """Provider-independent representation consumed by :class:`ScanEngine`.

    ``canada_remote_eligibility`` is tri-state: true/false only when the
    provider exposes structured, conclusive evidence; otherwise ``None``.
    ``publication_is_verified`` asserts that the adapter has verified the
    timestamp's documented publication semantics.
    """

    provider_job_id: str
    title: str
    description: str
    location: str
    application_url: str
    company: str | None = None
    workplace_type: WorkplaceType = WorkplaceType.UNKNOWN
    canada_remote_eligibility: bool | None = None
    published_at: datetime | None = None
    publication_source: str | None = None
    publication_semantics: PublicationSemantics | None = None
    publication_is_verified: bool = False

    def __post_init__(self) -> None:
        for name in ("provider_job_id", "title", "application_url"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must not be blank")
            object.__setattr__(self, name, value.strip())
        object.__setattr__(self, "description", self.description or "")
        object.__setattr__(self, "location", self.location or "")
        if self.company is not None:
            company = self.company.strip()
            object.__setattr__(self, "company", company or None)
        object.__setattr__(self, "workplace_type", WorkplaceType(self.workplace_type))
        if self.publication_semantics is not None:
            object.__setattr__(
                self,
                "publication_semantics",
                PublicationSemantics(self.publication_semantics),
            )
        if self.published_at is None:
            if self.publication_source is not None or self.publication_semantics is not None:
                raise ValueError("publication metadata requires published_at")
            if self.publication_is_verified:
                raise ValueError("verified publication evidence requires published_at")
        else:
            object.__setattr__(self, "published_at", as_utc(self.published_at))
            if not self.publication_source or not self.publication_source.strip():
                raise ValueError("published_at requires publication_source")
            object.__setattr__(self, "publication_source", self.publication_source.strip())
            if self.publication_semantics is None:
                raise ValueError("published_at requires publication_semantics")
            if (
                self.publication_is_verified
                and self.publication_semantics is PublicationSemantics.UNKNOWN
            ):
                raise ValueError("verified publication evidence cannot have unknown semantics")


@dataclass(frozen=True, slots=True)
class LocationDecision:
    match: LocationMatch
    workplace_type: WorkplaceType
    canada_remote_eligibility: bool | None
    reason: str


@dataclass(frozen=True, slots=True)
class EnginePolicy:
    recent_window: timedelta = timedelta(hours=24)
    snapshot_max_gap: timedelta = timedelta(hours=24)
    verified_publication_retention: timedelta = timedelta(days=60)
    retention_delete_batch_size: int = 500
    stale_source_after: timedelta | None = None
    repost_similarity_threshold: float = 0.92
    repost_title_similarity_threshold: float = 0.95
    allowed_application_statuses: frozenset[ApplicationStatus] = field(
        default_factory=lambda: frozenset(ApplicationStatus)
    )

    def __post_init__(self) -> None:
        if self.recent_window <= timedelta(0):
            raise ValueError("recent_window must be positive")
        if self.snapshot_max_gap <= timedelta(0):
            raise ValueError("snapshot_max_gap must be positive")
        if self.verified_publication_retention <= timedelta(0):
            raise ValueError("verified_publication_retention must be positive")
        if self.retention_delete_batch_size <= 0:
            raise ValueError("retention_delete_batch_size must be positive")
        if self.stale_source_after is not None and self.stale_source_after <= timedelta(0):
            raise ValueError("stale_source_after must be positive when configured")
        for name in ("repost_similarity_threshold", "repost_title_similarity_threshold"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        statuses = frozenset(ApplicationStatus(item) for item in self.allowed_application_statuses)
        if not statuses:
            raise ValueError("allowed_application_statuses must not be empty")
        object.__setattr__(self, "allowed_application_statuses", statuses)


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: int
    source_id: int
    composite_identity: str
    provider_job_id: str
    company: str
    title: str
    description: str
    location: str
    workplace_type: WorkplaceType
    canada_remote_eligibility: bool | None
    location_match: LocationMatch
    location_reason: str
    java_keyword_match: bool
    published_at: datetime | None
    publication_source: str | None
    publication_semantics: PublicationSemantics | None
    publication_is_verified: bool
    first_seen_at: datetime
    last_seen_at: datetime
    current_state: JobState
    removed_at: datetime | None
    application_url: str
    application_status: ApplicationStatus
    content_fingerprint: str
    discovery_type: DiscoveryType
    discovery_at: datetime
    discovery_previous_scan_at: datetime | None
    discovery_snapshot_gap_seconds: float | None
    potential_repost_of_job_id: int | None
    repost_similarity: float | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ScanRun:
    id: int
    source_id: int
    started_at: datetime
    ended_at: datetime
    completion_status: ScanStatus
    pagination_complete: bool
    record_count: int
    error_information: str | None
    previous_complete_scan_id: int | None
    previous_complete_scan_at: datetime | None
    snapshot_gap_seconds: float | None


@dataclass(frozen=True, slots=True)
class JobObservation:
    id: int
    scan_id: int
    job_id: int
    observation_timestamp: datetime
    observation_status: ObservationStatus
    is_present: bool
    content_fingerprint: str
    discovery_type: DiscoveryType
    comparison_scan_id: int | None
    comparison_scan_at: datetime | None
    snapshot_gap_seconds: float | None
    title: str
    description: str
    location: str
    workplace_type: WorkplaceType
    canada_remote_eligibility: bool | None
    location_match: LocationMatch
    location_reason: str
    java_keyword_match: bool
    published_at: datetime | None
    publication_source: str | None
    publication_semantics: PublicationSemantics | None
    publication_is_verified: bool
    application_url: str
    potential_repost_of_job_id: int | None
    repost_similarity: float | None


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    scan_id: int
    source_id: int
    status: ScanStatus
    previous_complete_scan_id: int | None
    previous_complete_scan_at: datetime | None
    snapshot_gap: timedelta | None
    record_count: int
    baseline: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    reappeared: tuple[str, ...] = ()
    potential_reposts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecentJob:
    job_id: int
    source_id: int
    company: str
    title: str
    location: str
    workplace_type: WorkplaceType
    discovery_type: DiscoveryType
    published_at: datetime | None
    publication_source: str | None
    publication_semantics: PublicationSemantics | None
    first_seen_at: datetime
    detection_at: datetime
    previous_board_scan_at: datetime | None
    snapshot_gap: timedelta | None
    provider: str
    board_identifier: str
    application_url: str
    application_status: ApplicationStatus


@dataclass(frozen=True, slots=True)
class CoverageStats:
    imported_sources: int
    successfully_scanned_sources: int
    failed_sources: int
    incomplete_sources: int
    not_yet_scanned_sources: int
    stale_sources: int | None
    total_jobs_inspected: int
    active_jobs: int
    jobs_with_verified_publication_timestamps: int
    newly_detected_jobs: int
    ambiguous_location_jobs: int
    insufficient_history_jobs: int

