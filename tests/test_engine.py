from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from ats_finder.db import Database
from ats_finder.engine import ScanEngine
from ats_finder.filters import classify_location, matches_java
from ats_finder.models import (
    ApplicationStatus,
    DiscoveryType,
    JobState,
    LocationMatch,
    NormalizedJob,
    ObservationStatus,
    PublicationSemantics,
    ScanStatus,
    SourceLastScanResult,
    WorkplaceType,
)

from conftest import FIXED_NOW, EngineHarness


JobFactory = Callable[..., NormalizedJob]


def _verified_job(
    job_factory: JobFactory,
    provider_job_id: str,
    *,
    published_at,
) -> NormalizedJob:
    return job_factory(
        provider_job_id,
        published_at=published_at,
        publication_source="Official fixture datePosted field",
        publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
        publication_is_verified=True,
    )


def test_first_run_initializes_baseline_without_claiming_newness(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    outcome = harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory("job-1"),
            job_factory(
                "job-2",
                title="Java Quality Engineer",
                description="Test Java platform releases.",
            ),
        ],
        completed_at=FIXED_NOW,
    )

    assert outcome.status is ScanStatus.SUCCESS
    assert outcome.previous_complete_scan_id is None
    assert outcome.baseline == ("job-1", "job-2")
    assert outcome.added == ()
    assert harness.engine.recent_jobs(now=FIXED_NOW) == []
    assert {job.discovery_type for job in harness.database.list_jobs()} == {
        DiscoveryType.BASELINE
    }
    assert {
        observation.observation_status
        for observation in harness.database.list_observations(scan_id=outcome.scan_id)
    } == {ObservationStatus.BASELINE}


def test_second_successful_run_detects_a_new_identifier(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    first = harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("job-1")],
        completed_at=FIXED_NOW,
    )
    second_at = FIXED_NOW + timedelta(hours=1)
    second = harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("job-1"), job_factory("job-2", title="Java Platform Engineer")],
        completed_at=second_at,
    )

    assert second.previous_complete_scan_id == first.scan_id
    assert second.previous_complete_scan_at == FIXED_NOW
    assert second.snapshot_gap == timedelta(hours=1)
    assert second.added == ("job-2",)
    added = harness.database.get_job_by_external_id(harness.source.id, "job-2")
    assert added is not None
    assert added.discovery_type is DiscoveryType.NEWLY_DETECTED
    assert added.first_seen_at == second_at
    assert added.discovery_previous_scan_at == FIXED_NOW


def test_existing_identifier_remains_unchanged(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    original = job_factory("job-1")
    first = harness.engine.process_complete_scan(
        harness.source.id, [original], completed_at=FIXED_NOW
    )
    second_at = FIXED_NOW + timedelta(hours=1)
    second = harness.engine.process_complete_scan(
        harness.source.id, [original], completed_at=second_at
    )

    assert second.unchanged == ("job-1",)
    assert second.changed == ()
    jobs = harness.database.list_jobs(source_id=harness.source.id)
    assert len(jobs) == 1
    assert jobs[0].first_seen_at == FIXED_NOW
    assert jobs[0].last_seen_at == second_at
    observations = harness.database.list_observations(job_id=jobs[0].id)
    assert [item.scan_id for item in observations] == [first.scan_id, second.scan_id]
    assert observations[-1].observation_status is ObservationStatus.UNCHANGED


def test_description_change_is_recorded_but_not_treated_as_new(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("job-1", description="Build Java services.")],
        completed_at=FIXED_NOW,
    )
    second_at = FIXED_NOW + timedelta(hours=1)
    outcome = harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("job-1", description="Build and operate Java 21 services.")],
        completed_at=second_at,
    )

    assert outcome.changed == ("job-1",)
    assert outcome.added == ()
    job = harness.database.get_job_by_external_id(harness.source.id, "job-1")
    assert job is not None
    assert job.description == "Build and operate Java 21 services."
    assert job.discovery_type is DiscoveryType.BASELINE
    assert job.first_seen_at == FIXED_NOW
    assert harness.database.list_observations(
        scan_id=outcome.scan_id
    )[0].observation_status is ObservationStatus.CHANGED
    assert harness.engine.recent_jobs(now=second_at) == []


def test_disappearing_job_is_marked_removed(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id, [job_factory("job-1")], completed_at=FIXED_NOW
    )
    removed_at = FIXED_NOW + timedelta(hours=1)
    outcome = harness.engine.process_complete_scan(
        harness.source.id, [], completed_at=removed_at
    )

    assert outcome.removed == ("job-1",)
    job = harness.database.get_job_by_external_id(harness.source.id, "job-1")
    assert job is not None
    assert job.current_state is JobState.REMOVED
    assert job.removed_at == removed_at
    observation = harness.database.list_observations(scan_id=outcome.scan_id)[0]
    assert observation.observation_status is ObservationStatus.REMOVED
    assert observation.is_present is False


def test_disappearing_job_reappears_as_history_not_a_brand_new_job(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    original = job_factory("job-1")
    harness.engine.process_complete_scan(
        harness.source.id, [original], completed_at=FIXED_NOW
    )
    harness.engine.process_complete_scan(
        harness.source.id, [], completed_at=FIXED_NOW + timedelta(hours=1)
    )
    reappeared_at = FIXED_NOW + timedelta(hours=2)
    outcome = harness.engine.process_complete_scan(
        harness.source.id, [original], completed_at=reappeared_at
    )

    assert outcome.reappeared == ("job-1",)
    assert outcome.added == ()
    job = harness.database.get_job_by_external_id(harness.source.id, "job-1")
    assert job is not None
    assert job.current_state is JobState.ACTIVE
    assert job.removed_at is None
    assert job.first_seen_at == FIXED_NOW
    assert job.last_seen_at == reappeared_at
    assert job.discovery_type is DiscoveryType.REAPPEARED
    assert len(harness.database.list_jobs(source_id=harness.source.id)) == 1
    assert harness.database.list_observations(
        scan_id=outcome.scan_id
    )[0].observation_status is ObservationStatus.REAPPEARED
    assert harness.engine.recent_jobs(now=reappeared_at) == []


def test_same_posting_with_new_identifier_is_flagged_as_potential_repost(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    first = job_factory("old-id")
    harness.engine.process_complete_scan(
        harness.source.id, [first], completed_at=FIXED_NOW
    )
    original = harness.database.get_job_by_external_id(harness.source.id, "old-id")
    assert original is not None

    second_at = FIXED_NOW + timedelta(hours=1)
    outcome = harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("new-id", application_url=first.application_url)],
        completed_at=second_at,
    )

    assert outcome.potential_reposts == ("new-id",)
    assert outcome.added == ()
    repost = harness.database.get_job_by_external_id(harness.source.id, "new-id")
    assert repost is not None
    assert repost.discovery_type is DiscoveryType.POTENTIAL_REPOST
    assert repost.potential_repost_of_job_id == original.id
    assert repost.repost_similarity == pytest.approx(1.0)
    assert harness.engine.recent_jobs(now=second_at) == []


def test_failed_scan_does_not_replace_the_last_complete_snapshot(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    baseline = harness.engine.process_complete_scan(
        harness.source.id, [job_factory("job-1")], completed_at=FIXED_NOW
    )
    failed = harness.engine.record_failed_scan(
        harness.source.id,
        "fixture timeout",
        completed_at=FIXED_NOW + timedelta(minutes=30),
        observed_record_count=1,
    )
    job_after_failure = harness.database.get_job_by_external_id(harness.source.id, "job-1")
    assert job_after_failure is not None
    assert failed.status is ScanStatus.FAILED
    assert failed.previous_complete_scan_id == baseline.scan_id
    assert harness.database.list_observations(scan_id=failed.scan_id) == []
    assert job_after_failure.current_state is JobState.ACTIVE

    completed_at = FIXED_NOW + timedelta(hours=1)
    next_success = harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("job-1"), job_factory("job-2", title="Java API Engineer")],
        completed_at=completed_at,
    )
    assert next_success.previous_complete_scan_id == baseline.scan_id
    assert next_success.snapshot_gap == timedelta(hours=1)
    assert next_success.added == ("job-2",)
    source = harness.database.get_source(harness.source.id)
    assert source is not None
    assert source.last_scan_result is SourceLastScanResult.SUCCESS
    assert source.last_successful_scan_at == completed_at


def test_incomplete_scan_does_not_replace_the_last_complete_snapshot(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    baseline = harness.engine.process_complete_scan(
        harness.source.id, [job_factory("job-1")], completed_at=FIXED_NOW
    )
    incomplete = harness.engine.record_incomplete_scan(
        harness.source.id,
        "fixture pagination ended early",
        completed_at=FIXED_NOW + timedelta(minutes=30),
        observed_record_count=1,
    )

    assert incomplete.status is ScanStatus.INCOMPLETE
    assert incomplete.previous_complete_scan_id == baseline.scan_id
    stored_scan = harness.database.get_scan_run(incomplete.scan_id)
    assert stored_scan is not None
    assert stored_scan.pagination_complete is False
    assert stored_scan.error_information == "fixture pagination ended early"
    assert harness.database.list_observations(scan_id=incomplete.scan_id) == []

    completed_at = FIXED_NOW + timedelta(hours=1)
    next_success = harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("job-1"), job_factory("job-2", title="Senior Java Engineer")],
        completed_at=completed_at,
    )
    assert next_success.previous_complete_scan_id == baseline.scan_id
    assert next_success.added == ("job-2",)
    assert next_success.removed == ()


def test_snapshot_gap_over_24_hours_is_time_uncertain_and_not_recent(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id, [job_factory("job-1")], completed_at=FIXED_NOW
    )
    completed_at = FIXED_NOW + timedelta(hours=24, seconds=1)
    outcome = harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory("job-1"),
            job_factory(
                "job-2",
                title="Java Security Engineer",
                description="Secure Java identity services.",
            ),
        ],
        completed_at=completed_at,
    )

    assert outcome.snapshot_gap == timedelta(hours=24, seconds=1)
    assert outcome.added == ("job-2",)
    job = harness.database.get_job_by_external_id(harness.source.id, "job-2")
    assert job is not None
    assert job.discovery_type is DiscoveryType.NEWLY_DETECTED_TIME_UNCERTAIN
    assert harness.engine.recent_jobs(now=completed_at) == []


def test_verified_publication_inside_24_hours_is_recent_on_initial_scan(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    published_at = FIXED_NOW - timedelta(hours=23, minutes=59)
    outcome = harness.engine.process_complete_scan(
        harness.source.id,
        [_verified_job(job_factory, "job-1", published_at=published_at)],
        completed_at=FIXED_NOW,
    )

    job = harness.database.get_job_by_external_id(harness.source.id, "job-1")
    assert job is not None
    assert outcome.baseline == ("job-1",)
    assert job.discovery_type is DiscoveryType.VERIFIED_RECENT
    recent = harness.engine.recent_jobs(now=FIXED_NOW)
    assert len(recent) == 1
    assert recent[0].discovery_type is DiscoveryType.VERIFIED_RECENT
    assert recent[0].published_at == published_at
    assert recent[0].publication_semantics is PublicationSemantics.ORIGINAL_PUBLICATION


def test_verified_publication_older_than_24_hours_is_not_recent(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    published_at = FIXED_NOW - timedelta(hours=24, seconds=1)
    harness.engine.process_complete_scan(
        harness.source.id,
        [_verified_job(job_factory, "job-1", published_at=published_at)],
        completed_at=FIXED_NOW,
    )

    job = harness.database.get_job_by_external_id(harness.source.id, "job-1")
    assert job is not None
    assert job.discovery_type is DiscoveryType.VERIFIED_OLD
    assert job.published_at == published_at
    assert harness.engine.recent_jobs(now=FIXED_NOW) == []


def test_missing_publication_date_remains_an_unverified_baseline(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id, [job_factory("job-1")], completed_at=FIXED_NOW
    )

    job = harness.database.get_job_by_external_id(harness.source.id, "job-1")
    assert job is not None
    assert job.published_at is None
    assert job.publication_source is None
    assert job.publication_semantics is None
    assert job.publication_is_verified is False
    assert job.discovery_type is DiscoveryType.BASELINE
    assert harness.engine.recent_jobs(now=FIXED_NOW) == []


def test_missing_original_date_can_qualify_by_new_snapshot_discovery(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    baseline = harness.engine.process_complete_scan(
        harness.source.id, [job_factory("job-1")], completed_at=FIXED_NOW
    )
    detected_at = FIXED_NOW + timedelta(hours=2)
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory("job-1"),
            job_factory(
                "job-2",
                title="Principal Java Data Engineer",
                description="Design Java data ingestion systems.",
                location="Remote - Canada",
            ),
        ],
        completed_at=detected_at,
    )

    recent = harness.engine.recent_jobs(now=detected_at)
    assert [job.title for job in recent] == ["Principal Java Data Engineer"]
    assert len(recent) == 1
    result = recent[0]
    assert result.discovery_type is DiscoveryType.NEWLY_DETECTED
    assert result.published_at is None
    assert result.publication_source is None
    assert result.first_seen_at == detected_at
    assert result.detection_at == detected_at
    assert result.previous_board_scan_at == FIXED_NOW
    assert result.snapshot_gap == timedelta(hours=2)
    assert baseline.scan_id is not None


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("Java Developer", "Services", True),
        ("Backend Engineer", "Build services with JAVA 17.", True),
        ("Platform Engineer", "Java-based services", True),
        ("JavaScript Developer", "React and JavaScript frameworks", False),
        ("JVM Engineer", "Spring and Kotlin", False),
    ],
)
def test_java_word_boundary_rejects_javascript_only(
    title: str,
    description: str,
    expected: bool,
) -> None:
    assert matches_java(title, description) is expected


@pytest.mark.parametrize(
    ("location", "expected_mode"),
    [
        ("Ottawa, Ontario", WorkplaceType.UNKNOWN),
        ("Kanata - Hybrid", WorkplaceType.HYBRID),
        ("Stittsville - Onsite", WorkplaceType.ONSITE),
    ],
)
def test_ottawa_area_locations_are_eligible(
    location: str,
    expected_mode: WorkplaceType,
) -> None:
    decision = classify_location(location, "", WorkplaceType.UNKNOWN)
    assert decision.match is LocationMatch.CONFIRMED
    assert decision.workplace_type is expected_mode
    assert decision.reason == "ottawa_area_location"


@pytest.mark.parametrize(
    "location",
    [
        "Remote - Canada",
        "Canada (Remote)",
        "Remote Ontario",
    ],
)
def test_explicit_canada_remote_locations_are_eligible(location: str) -> None:
    decision = classify_location(location)
    assert decision.match is LocationMatch.CONFIRMED
    assert decision.workplace_type is WorkplaceType.REMOTE
    assert decision.canada_remote_eligibility is True
    assert decision.reason == "canada_remote_explicit"


def test_structured_canada_remote_eligibility_is_accepted() -> None:
    decision = classify_location(
        "Distributed",
        workplace_type=WorkplaceType.REMOTE,
        canada_remote_eligibility=True,
    )
    assert decision.match is LocationMatch.CONFIRMED
    assert decision.workplace_type is WorkplaceType.REMOTE
    assert decision.canada_remote_eligibility is True
    assert decision.reason == "structured_canada_remote_eligible"


@pytest.mark.parametrize(
    ("location", "description"),
    [
        ("Remote - United States only", "",),
        ("Remote", "Candidates must reside in the U.S.",),
    ],
)
def test_us_only_remote_is_excluded(location: str, description: str) -> None:
    decision = classify_location(location, description)
    assert decision.match is LocationMatch.REJECTED
    assert decision.canada_remote_eligibility is False
    assert decision.reason == "us_only_remote"


def test_us_only_remote_is_not_overridden_by_incidental_canada_text() -> None:
    decision = classify_location(
        "Remote - United States only",
        "The company also serves customers in Canada.",
    )

    assert decision.match is LocationMatch.REJECTED
    assert decision.reason == "us_only_remote"


def test_non_ottawa_hybrid_is_not_overridden_by_remote_boilerplate() -> None:
    decision = classify_location(
        "Toronto - Hybrid",
        "Our company also has remote employees across Canada.",
    )

    assert decision.match is LocationMatch.REJECTED
    assert decision.workplace_type is WorkplaceType.HYBRID
    assert decision.reason == "non_ottawa_hybrid_or_onsite"


def test_content_and_application_audit_streams_are_immutable(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    first = harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("job-1", description="Build Java services.")],
        completed_at=FIXED_NOW,
    )
    second = harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("job-1", description="Build Java 21 services.")],
        completed_at=FIXED_NOW + timedelta(hours=1),
    )
    job = harness.database.get_job_by_external_id(harness.source.id, "job-1")
    assert job is not None

    versions = harness.database.list_job_versions(job_id=job.id)
    assert [item["description"] for item in versions] == [
        "Build Java 21 services.",
        "Build Java services.",
    ]
    events = harness.database.list_job_events(job_id=job.id)
    assert [item["event_type"] for item in events] == ["CHANGED", "BASELINE"]
    assert [item["scan_id"] for item in events] == [second.scan_id, first.scan_id]

    changed_at = FIXED_NOW + timedelta(hours=2)
    harness.engine.set_application_status(
        job.id,
        ApplicationStatus.APPLIED,
        now=changed_at,
    )
    harness.engine.set_application_status(
        job.id,
        ApplicationStatus.APPLIED,
        now=changed_at + timedelta(minutes=1),
    )
    history = harness.database.list_application_status_history(job_id=job.id)
    assert len(history) == 2
    assert history[0]["previous_status"] == ApplicationStatus.NOT_APPLIED.value
    assert history[0]["new_status"] == ApplicationStatus.APPLIED.value
    assert history[0]["change_kind"] == "USER_UPDATE"
    assert history[1]["new_status"] == ApplicationStatus.NOT_APPLIED.value
    assert history[1]["change_kind"] == "JOB_CREATED"


def test_snapshot_history_persists_across_database_restart(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    first = harness.engine.process_complete_scan(
        harness.source.id, [job_factory("job-1")], completed_at=FIXED_NOW
    )
    path = harness.path
    source_id = harness.source.id
    harness.database.close()

    restarted_database = Database(path)
    try:
        restarted_engine = ScanEngine(restarted_database)
        second_at = FIXED_NOW + timedelta(hours=1)
        second = restarted_engine.process_complete_scan(
            source_id,
            [job_factory("job-1"), job_factory("job-2", title="Java Data Engineer")],
            completed_at=second_at,
        )

        assert second.previous_complete_scan_id == first.scan_id
        assert second.added == ("job-2",)
        assert len(restarted_database.list_jobs(source_id=source_id)) == 2
        added = restarted_database.get_job_by_external_id(source_id, "job-2")
        assert added is not None
        assert added.discovery_type is DiscoveryType.NEWLY_DETECTED
        assert [job.job_id for job in restarted_engine.recent_jobs(now=second_at)] == [
            added.id
        ]
    finally:
        restarted_database.close()


def test_repeated_complete_scans_do_not_duplicate_jobs(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    job = job_factory("job-1")
    outcomes = [
        harness.engine.process_complete_scan(
            harness.source.id,
            [job],
            completed_at=FIXED_NOW + timedelta(hours=offset),
        )
        for offset in range(3)
    ]

    jobs = harness.database.list_jobs(source_id=harness.source.id)
    assert len(jobs) == 1
    observations = harness.database.list_observations(job_id=jobs[0].id)
    assert len(observations) == 3
    assert [item.observation_status for item in observations] == [
        ObservationStatus.BASELINE,
        ObservationStatus.UNCHANGED,
        ObservationStatus.UNCHANGED,
    ]
    assert len({item.scan_id for item in observations}) == 3
    assert len(harness.database.list_scan_runs(source_id=harness.source.id)) == 3
    assert [outcome.record_count for outcome in outcomes] == [1, 1, 1]


def test_duplicate_ids_in_one_claimed_complete_snapshot_are_rejected_atomically(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    duplicate = job_factory("duplicate-id")

    with pytest.raises(ValueError, match="duplicate provider job ids"):
        harness.engine.process_complete_scan(
            harness.source.id,
            [duplicate, duplicate],
            completed_at=FIXED_NOW,
        )

    assert harness.database.list_scan_runs(source_id=harness.source.id) == []
    assert harness.database.list_jobs(source_id=harness.source.id) == []

