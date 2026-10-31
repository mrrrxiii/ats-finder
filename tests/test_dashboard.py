from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ats_finder.dashboard import (
    DEFAULT_RECENT_WINDOW,
    JOB_DETAIL_SELECTION_KEY,
    RECENT_WINDOW_OPTIONS,
    _history_frame,
    coverage_progress,
    discovery_label,
    evidence_age_label,
    freshness_cell_style,
    freshness_label,
    posting_status_label,
    recent_job_row,
    row_pick_job_id,
)
from ats_finder.models import (
    DiscoveryType,
    NormalizedJob,
    PublicationSemantics,
    SourceInput,
    SourceSupportStatus,
)
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
    assert [item.value for item in app.subheader[:3]] == [
        "Posted dates from the ATS",
        "New to this site",
        "First observed by this site",
    ]
    assert app.selectbox(key="recent_window").value == DEFAULT_RECENT_WINDOW

    app.selectbox(key="recent_window").set_value("24 hours").run(timeout=10)
    assert not app.exception
    assert app.selectbox(key="recent_window").value == "24 hours"

    for page in ("Scan Coverage", "Job History", "Latest Java Openings"):
        app.radio[0].set_value(page).run(timeout=10)
        assert not app.exception

    app.radio[0].set_value("Job History").run(timeout=10)
    assert [item.label for item in app.text_input] == [
        "Company",
        "Job title",
        "Location",
        "Job description",
    ]
    assert any(item.label == "Whole-word search" for item in app.toggle)


def test_recent_window_options_are_ordered_and_include_72_hours() -> None:
    assert DEFAULT_RECENT_WINDOW == "72 hours"
    assert list(RECENT_WINDOW_OPTIONS) == ["24 hours", "72 hours", "7 days", "30 days"]
    assert RECENT_WINDOW_OPTIONS["72 hours"] == timedelta(hours=72)


def test_history_filtering_and_limit_run_before_materializing_results(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory("java", title="Java Engineer", location="Ottawa"),
            job_factory(
                "python",
                title="Python Engineer",
                description="Build Python services.",
                location="Toronto",
            ),
        ],
        completed_at=FIXED_NOW,
    )

    frame = _history_frame(
        harness.database,
        title="java engineer",
        state="ACTIVE",
        limit=1,
    )

    assert frame["Job Title"].tolist() == ["Java Engineer"]
    assert frame["Job URL"].tolist() == [
        "https://boards.greenhouse.io/example-company/jobs/java"
    ]
    assert list(frame.columns).index("Job URL") == 4
    assert "Job Description" not in frame.columns
    assert "ATS" not in frame.columns

    ottawa = _history_frame(
        harness.database,
        title="engineer",
        location="ottawa",
        state="ACTIVE",
        limit=10,
    )
    assert ottawa["Job Title"].tolist() == ["Java Engineer"]

    description = _history_frame(
        harness.database,
        description="java services",
        state="ACTIVE",
        limit=10,
    )
    assert description["Job Title"].tolist() == ["Java Engineer"]

    no_match = _history_frame(
        harness.database,
        company="Example Company",
        title="python",
        location="ottawa",
        description="java services",
        state="ACTIVE",
        limit=10,
    )
    assert no_match.empty


def test_history_search_can_match_java_as_a_whole_word(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                "java",
                title="Java Engineer",
                description="Build Java platform services.",
            ),
            job_factory(
                "javascript",
                title="JavaScript Engineer",
                description="Build JavaScript browser applications.",
            ),
        ],
        completed_at=FIXED_NOW,
    )

    substring = _history_frame(
        harness.database,
        title="Java",
        description="Java",
        state="ACTIVE",
        limit=10,
    )
    whole_word = _history_frame(
        harness.database,
        title="Java",
        description="Java",
        whole_word=True,
        state="ACTIVE",
        limit=10,
    )

    assert set(substring["Job Title"]) == {
        "Java Engineer",
        "JavaScript Engineer",
    }
    assert whole_word["Job Title"].tolist() == ["Java Engineer"]

    both_terms = _history_frame(
        harness.database,
        description="Java platform",
        whole_word=True,
        state="ACTIVE",
        limit=10,
    )
    assert both_terms["Job Title"].tolist() == ["Java Engineer"]


def test_72_hour_window_lists_publication_the_default_policy_hides(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                "two-day-old",
                published_at=FIXED_NOW - timedelta(hours=48),
                publication_source="Official fixture datePosted field",
                publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
                publication_is_verified=True,
            )
        ],
        completed_at=FIXED_NOW,
    )

    assert harness.engine.recent_jobs(now=FIXED_NOW) == []
    widened = harness.engine.recent_jobs(now=FIXED_NOW, window=timedelta(hours=72))

    assert [job.job_id for job in widened] == [
        harness.database.get_job_by_external_id(harness.source.id, "two-day-old").id
    ]
    assert widened[0].discovery_type is DiscoveryType.VERIFIED_RECENT
    assert evidence_age_label(widened[0], now=FIXED_NOW) == "Published 2d ago"


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
    assert posting_status_label(job) == "New posting"
    assert row["Status"] == "New posting · 2h ago"
    assert row["Published At"] == published_at
    assert row["First Detected"] == FIXED_NOW


def test_republished_job_is_marked_as_not_brand_new(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    first_seen = FIXED_NOW - timedelta(days=2)
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                "republished-job",
                published_at=FIXED_NOW - timedelta(days=10),
                publication_source="Official fixture publishedAt field",
                publication_semantics=PublicationSemantics.REPUBLICATION,
                publication_is_verified=True,
            )
        ],
        completed_at=first_seen,
    )
    detected_at = FIXED_NOW
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                "republished-job",
                published_at=FIXED_NOW - timedelta(hours=1),
                publication_source="Official fixture publishedAt field",
                publication_semantics=PublicationSemantics.REPUBLICATION,
                publication_is_verified=True,
            )
        ],
        completed_at=detected_at,
    )

    job = harness.engine.recent_jobs(now=FIXED_NOW)[0]
    row = recent_job_row(job, now=FIXED_NOW)

    assert job.first_seen_at == first_seen
    assert job.discovery_type is DiscoveryType.VERIFIED_RECENT
    assert posting_status_label(job) == "Reposted"
    assert evidence_age_label(job, now=FIXED_NOW) == "Republished 1h ago"
    assert freshness_label(job, now=FIXED_NOW) == "Reposted · 1h ago"
    assert row["Status"] == "Reposted · 1h ago"


def test_first_observed_ashby_publication_is_a_new_posting(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    first_seen = FIXED_NOW - timedelta(hours=2)
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                "first-seen",
                published_at=FIXED_NOW - timedelta(hours=12),
                publication_source="Official fixture publishedAt field",
                publication_semantics=PublicationSemantics.REPUBLICATION,
                publication_is_verified=True,
            )
        ],
        completed_at=first_seen,
    )

    job = harness.engine.recent_jobs(now=FIXED_NOW)[0]

    assert posting_status_label(job) == "New posting"
    assert freshness_label(job, now=FIXED_NOW) == "New posting · 12h ago"


def test_freshness_labels_have_distinct_supplementary_colors() -> None:
    labels = [
        "New posting · 1h ago",
        "Reposted · 1h ago",
        "Relisted · 1h ago",
        "New to this site · 1h ago",
        "New to this site · timing uncertain · 1h ago",
        "First seen here · 1h ago",
    ]

    styles = [freshness_cell_style(label) for label in labels]

    assert all(styles)
    assert len(set(styles)) == len(styles)
    assert freshness_cell_style("Unknown") == ""


def test_latest_table_columns_start_with_company_title_publication_and_apply(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                "verified-job",
                published_at=FIXED_NOW - timedelta(hours=2),
                publication_source="Official fixture datePosted field",
                publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
                publication_is_verified=True,
            )
        ],
        completed_at=FIXED_NOW,
    )
    job = harness.engine.recent_jobs(now=FIXED_NOW)[0]

    row = recent_job_row(job, now=FIXED_NOW)

    assert list(row)[:4] == ["Company", "Job Title", "Published At", "Job URL"]
    assert list(row)[-1] == "Job ID"
    assert str(row["Job URL"]).endswith("/verified-job")


def _jobs_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Company": ["Example Company", "Example Company", "Example Company"],
            "Job Title": [
                "Alpha Java Engineer",
                "Beta Java Engineer",
                "Gamma Java Engineer",
            ],
            "Job ID": [11, 22, 33],
        }
    )


def test_row_pick_maps_clicked_row_to_its_own_job_id() -> None:
    frame = _jobs_frame()

    assert row_pick_job_id(frame, [0]) == 11
    assert row_pick_job_id(frame, [2]) == 33
    assert row_pick_job_id(frame, []) is None
    assert row_pick_job_id(frame, [7]) is None
    assert row_pick_job_id(frame.drop(columns=["Job ID"]), [0]) is None


def test_clicking_a_table_row_selects_that_job(monkeypatch) -> None:
    import ats_finder.dashboard as dashboard

    class _Selection:
        rows = [1]

    class _Event:
        selection = _Selection()

    monkeypatch.setattr(dashboard.st, "dataframe", lambda *args, **kwargs: _Event())
    monkeypatch.setattr(dashboard.st, "download_button", lambda *args, **kwargs: None)
    monkeypatch.setitem(dashboard.st.session_state, dashboard.ROW_PICK_STATE_KEY, None)

    picked: list[int] = []

    dashboard._render_job_table(
        _jobs_frame(),
        key="verified_recent_java_jobs",
        on_row_pick=picked.append,
    )

    assert picked == [22]


def test_job_detail_keeps_the_chosen_job_when_new_results_arrive(
    harness: EngineHarness,
    job_factory: JobFactory,
    monkeypatch,
) -> None:
    published = datetime.now(timezone.utc) - timedelta(minutes=30)
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                provider_job_id,
                title=title,
                published_at=published - timedelta(minutes=offset),
                publication_source="Official fixture datePosted field",
                publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
                publication_is_verified=True,
            )
            for provider_job_id, title, offset in (
                ("alpha", "Alpha Java Engineer", 0),
                ("beta", "Beta Java Engineer", 10),
                ("gamma", "Gamma Java Engineer", 20),
            )
        ],
        completed_at=published,
    )
    monkeypatch.setenv("ATS_FINDER_DB", str(harness.path))
    entry_point = Path(__file__).parents[1] / "streamlit_app.py"
    app = AppTest.from_file(str(entry_point)).run(timeout=30)

    picker = app.selectbox(key=JOB_DETAIL_SELECTION_KEY)
    assert picker.options == [
        "Example Company — Alpha Java Engineer",
        "Example Company — Beta Java Engineer",
        "Example Company — Gamma Java Engineer",
    ]
    gamma_id = harness.database.get_job_by_external_id(harness.source.id, "gamma").id

    picker.set_value(str(gamma_id)).run(timeout=30)
    assert not app.exception
    assert app.selectbox(key=JOB_DETAIL_SELECTION_KEY).value == str(gamma_id)
    assert [item.value for item in app.markdown if item.value.startswith("### ")] == [
        "### Gamma Java Engineer"
    ]

    # A worker scan publishes one more matching job while the page is open.
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                provider_job_id,
                title=title,
                published_at=published - timedelta(minutes=offset),
                publication_source="Official fixture datePosted field",
                publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
                publication_is_verified=True,
            )
            for provider_job_id, title, offset in (
                ("alpha", "Alpha Java Engineer", 0),
                ("beta", "Beta Java Engineer", 10),
                ("gamma", "Gamma Java Engineer", 20),
            )
        ]
        + [
            job_factory(
                "delta",
                title="Delta Java Engineer",
                published_at=datetime.now(timezone.utc),
                publication_source="Official fixture datePosted field",
                publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
                publication_is_verified=True,
            )
        ],
        completed_at=datetime.now(timezone.utc),
    )
    app.run(timeout=30)

    assert not app.exception
    assert app.selectbox(key=JOB_DETAIL_SELECTION_KEY).value == str(gamma_id)
    assert [item.value for item in app.markdown if item.value.startswith("### ")] == [
        "### Gamma Java Engineer"
    ]


def test_job_detail_falls_back_when_the_chosen_job_leaves_the_window(
    harness: EngineHarness,
    job_factory: JobFactory,
    monkeypatch,
) -> None:
    published = datetime.now(timezone.utc) - timedelta(minutes=30)
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                "fresh",
                title="Fresh Java Engineer",
                published_at=published,
                publication_source="Official fixture datePosted field",
                publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
                publication_is_verified=True,
            ),
            job_factory(
                "older",
                title="Older Java Engineer",
                published_at=published - timedelta(hours=48),
                publication_source="Official fixture datePosted field",
                publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
                publication_is_verified=True,
            ),
        ],
        completed_at=published,
    )
    monkeypatch.setenv("ATS_FINDER_DB", str(harness.path))
    entry_point = Path(__file__).parents[1] / "streamlit_app.py"
    app = AppTest.from_file(str(entry_point)).run(timeout=30)

    older_id = harness.database.get_job_by_external_id(harness.source.id, "older").id
    app.selectbox(key=JOB_DETAIL_SELECTION_KEY).set_value(str(older_id)).run(timeout=30)
    assert not app.exception
    assert [item.value for item in app.markdown if item.value.startswith("### ")] == [
        "### Older Java Engineer"
    ]

    app.selectbox(key="recent_window").set_value("24 hours").run(timeout=30)

    assert not app.exception
    assert [item.value for item in app.markdown if item.value.startswith("### ")] == [
        "### Fresh Java Engineer"
    ]


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
    assert len(recent) == 2
    job = next(
        item for item in recent if item.discovery_type is DiscoveryType.NEWLY_DETECTED
    )
    assert job.discovery_type is DiscoveryType.NEWLY_DETECTED

    display_at = detected_at + timedelta(hours=2)
    assert discovery_label(job.discovery_type) == (
        "Newly detected by snapshot comparison"
    )
    assert evidence_age_label(job, now=display_at) == "First detected 2h ago"
    row = recent_job_row(job, now=display_at)
    assert posting_status_label(job) == "New to this site"
    assert row["Status"] == "New to this site · 2h ago"
    assert "Published" not in str(row["Status"])
    assert row["Published At"] is None
    assert row["First Detected"] == detected_at
    assert job.previous_board_scan_at == FIXED_NOW


def test_undated_baseline_match_uses_first_observation_as_candidate(
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    harness.engine.process_complete_scan(
        harness.source.id,
        [job_factory("baseline-job", title="Java Platform Engineer")],
        completed_at=FIXED_NOW,
    )

    jobs = harness.engine.recent_jobs(now=FIXED_NOW)

    assert len(jobs) == 1
    assert jobs[0].discovery_type is DiscoveryType.BASELINE
    assert jobs[0].published_at is None
    assert jobs[0].detection_at == FIXED_NOW
    assert evidence_age_label(jobs[0], now=FIXED_NOW) == "First detected 0s ago"
    assert freshness_label(jobs[0], now=FIXED_NOW) == "First seen here · 0s ago"


def test_coverage_progress_counts_records_only_over_supported_boards() -> None:
    now = FIXED_NOW
    frame = pd.DataFrame(
        [
            {
                "Support": SourceSupportStatus.SUPPORTED.value,
                "Excluded": False,
                "Canada Presence": "SOME",
                "Latest Result": "SUCCESS",
                "Last Successful Scan": now - timedelta(minutes=30),
            },
            {
                "Support": SourceSupportStatus.SUPPORTED.value,
                "Excluded": False,
                "Canada Presence": "SOME",
                "Latest Result": "SUCCESS",
                "Last Successful Scan": now - timedelta(hours=5),
            },
            {
                "Support": SourceSupportStatus.SUPPORTED.value,
                "Excluded": False,
                "Canada Presence": "SOME",
                "Latest Result": "FAILED",
                "Last Successful Scan": now - timedelta(days=2),
            },
            {
                "Support": SourceSupportStatus.SUPPORTED.value,
                "Excluded": False,
                "Canada Presence": "SOME",
                "Latest Result": "NOT_SCANNED",
                "Last Successful Scan": None,
            },
            {
                "Support": SourceSupportStatus.UNSUPPORTED.value,
                "Excluded": False,
                "Canada Presence": "SOME",
                "Latest Result": "NOT_SCANNED",
                "Last Successful Scan": None,
            },
        ]
    )

    progress = coverage_progress(frame, now=now, stale_after=timedelta(hours=2))

    assert progress.supported == 4
    assert progress.covered == 3
    assert progress.attempted == 3
    assert progress.failing == 1
    assert progress.fresh == 1
    assert progress.coverage_ratio == 0.75
    assert progress.fresh_ratio == 0.25


def test_coverage_progress_ignores_operator_excluded_boards() -> None:
    now = FIXED_NOW
    frame = pd.DataFrame(
        [
            {
                "Support": SourceSupportStatus.SUPPORTED.value,
                "Excluded": False,
                "Canada Presence": "SOME",
                "Latest Result": "SUCCESS",
                "Last Successful Scan": now - timedelta(minutes=5),
            },
            {
                "Support": SourceSupportStatus.SUPPORTED.value,
                "Excluded": True,
                "Canada Presence": "SOME",
                "Latest Result": "NOT_SCANNED",
                "Last Successful Scan": None,
            },
        ]
    )

    progress = coverage_progress(frame, now=now, stale_after=timedelta(hours=2))

    assert progress.supported == 1
    assert progress.covered == 1
    assert progress.attempted == 1
    assert progress.coverage_ratio == 1.0
    assert progress.fresh_ratio == 1.0


def test_coverage_progress_ignores_no_canada_boards() -> None:
    now = FIXED_NOW
    frame = pd.DataFrame(
        [
            {
                "Support": SourceSupportStatus.SUPPORTED.value,
                "Excluded": False,
                "Canada Presence": "SOME",
                "Latest Result": "SUCCESS",
                "Last Successful Scan": now - timedelta(minutes=5),
            },
            {
                "Support": SourceSupportStatus.SUPPORTED.value,
                "Excluded": False,
                "Canada Presence": "NONE",
                "Latest Result": "SUCCESS",
                "Last Successful Scan": now - timedelta(days=1),
            },
        ]
    )

    progress = coverage_progress(frame, now=now, stale_after=timedelta(hours=2))

    assert progress.supported == 1
    assert progress.covered == 1
    assert progress.attempted == 1
    assert progress.coverage_ratio == 1.0
    assert progress.fresh_ratio == 1.0


def test_coverage_progress_is_zero_without_supported_boards() -> None:
    frame = pd.DataFrame(
        [
            {
                "Support": SourceSupportStatus.UNSUPPORTED.value,
                "Excluded": False,
                "Canada Presence": "SOME",
                "Latest Result": "NOT_SCANNED",
                "Last Successful Scan": None,
            }
        ]
    )

    progress = coverage_progress(frame, now=FIXED_NOW, stale_after=timedelta(hours=2))

    assert progress.supported == 0
    assert progress.coverage_ratio == 0.0
    assert progress.fresh_ratio == 0.0

    empty = coverage_progress(
        pd.DataFrame(), now=FIXED_NOW, stale_after=timedelta(hours=2)
    )
    assert empty.supported == 0
    assert empty.coverage_ratio == 0.0


def test_coverage_page_reports_scan_progress_over_supported_boards(
    monkeypatch,
    harness: EngineHarness,
    job_factory: JobFactory,
) -> None:
    published = FIXED_NOW - timedelta(hours=1)
    harness.engine.process_complete_scan(
        harness.source.id,
        [
            job_factory(
                "java-role",
                published_at=published,
                publication_source="Official fixture datePosted field",
                publication_semantics=PublicationSemantics.ORIGINAL_PUBLICATION,
                publication_is_verified=True,
            )
        ],
    )
    harness.database.upsert_source(
        SourceInput(
            provider="greenhouse",
            company="Never Scanned Company",
            board_identifier="never-scanned-company",
            career_page_url="https://boards.greenhouse.io/never-scanned-company",
            support_status=SourceSupportStatus.SUPPORTED,
        ),
        now=FIXED_NOW,
    )
    harness.database.upsert_source(
        SourceInput(
            provider="unsupported-ats",
            company="Unsupported Company",
            board_identifier="unsupported-company",
            career_page_url="https://example.invalid/unsupported-company",
            support_status=SourceSupportStatus.UNSUPPORTED,
        ),
        now=FIXED_NOW,
    )
    monkeypatch.setenv("ATS_FINDER_DB", str(harness.path))
    entry_point = Path(__file__).parents[1] / "streamlit_app.py"
    app = AppTest.from_file(str(entry_point)).run(timeout=30)
    app.radio[0].set_value("Scan Coverage").run(timeout=30)

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Coverage"] == "50.0%"
    assert metrics["Supported boards covered"] == "1 of 2"
    assert metrics["Fresh within stale window"] == "50.0%"
    assert app.progress[0].value == 50
    assert app.progress[0].text == (
        "1 of 2 eligible supported boards scanned successfully at least once"
    )

