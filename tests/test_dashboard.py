from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from ats_finder.dashboard import discovery_label, evidence_age_label, recent_job_row
from ats_finder.models import DiscoveryType, NormalizedJob, PublicationSemantics
from streamlit.testing.v1 import AppTest

from conftest import FIXED_NOW, EngineHarness


JobFactory = Callable[..., NormalizedJob]


def test_streamlit_entry_point_renders_all_pages_without_exceptions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ATS_FINDER_DB", str(tmp_path / "dashboard.sqlite3"))
    entry_point = Path(__file__).parents[1] / "streamlit_app.py"
    app = AppTest.from_file(str(entry_point)).run(timeout=10)

    assert not app.exception
    assert app.title[0].value == "Canada Java ATS Finder"
    assert app.radio[0].options == [
        "Latest Java Openings",
        "Scan Coverage",
        "Job History",
    ]

    for page in ("Scan Coverage", "Job History", "Latest Java Openings"):
        app.radio[0].set_value(page).run(timeout=10)
        assert not app.exception


def test_verified_recent_dashboard_row_labels_publication_evidence(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    published_at = FIXED_NOW - timedelta(hours=2)
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                "verified-job",
                published_at=published_at,
                publication_source="Official fixture datePosted field",
                publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
                publication_is_verified=True,
            )
        ],
        completed_at=FIXED_NOW,
    )
    job = harness.engine.recent_jobs(now=FIXED_NOW)[0]

    assert discovery_label(job.discovery_type) == "Verified recent publication"
    assert evidence_age_label(job, now=FIXED_NOW) == "Published 2h ago"
    row = recent_job_row(job, now=FIXED_NOW)
    assert row["Discovery Type"] == "Verified recent publication"
    assert row["Evidence Age"] == "Published 2h ago"
    assert row["Published At"] == published_at
    assert row["First Seen"] == FIXED_NOW


def test_snapshot_discovery_dashboard_row_never_labels_first_seen_as_published(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("baseline-job")],
        completed_at=FIXED_NOW,
    )
    detected_at = FIXED_NOW + timedelta(hours=1)
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory("baseline-job"),
            job_factory(
                "snapshot-job",
                title="Senior Java Runtime Engineer",
                description="Develop Java runtime diagnostics.",
            ),
        ],
        completed_at=detected_at,
    )
    recent = harness.engine.recent_jobs(now=detected_at)
    assert len(recent) == 1
    job = recent[0]
    assert job.discovery_type is DiscoveryType.NEWLY_DETECTED

    display_at = detected_at + timedelta(hours=2)
    assert discovery_label(job.discovery_type) == (
        "Newly detected by snapshot comparison"
    )
    assert evidence_age_label(job, now=display_at) == "First detected 2h ago"
    row = recent_job_row(job, now=display_at)
    assert row["Discovery Type"] == "Newly detected by snapshot comparison"
    assert row["Evidence Age"] == "First detected 2h ago"
    assert "Published" not in str(row["Evidence Age"])
    assert row["Published At"] is None
    assert row["First Seen"] == detected_at
    assert row["Previous Board Scan"] == FIXED_NOW

