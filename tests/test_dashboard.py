from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ats_finder.dashboard import (
    DEFAULT_RECENT_WINDOW,
    JOB_DETAIL_SELECTION_KEY,
    RECENT_WINDOW_OPTIONS,
    discovery_label,
    evidence_age_label,
    recent_job_row,
    row_pick_job_id,
)
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
    assert app.selectbox(key="recent_window").value == DEFAULT_RECENT_WINDOW

    app.selectbox(key="recent_window").set_value("24 hours").run(timeout=10)
    assert not app.exception
    assert app.selectbox(key="recent_window").value == "24 hours"

    for page in ("Scan Coverage", "Job History", "Latest Java Openings"):
        app.radio[0].set_value(page).run(timeout=10)
        assert not app.exception


def test_recent_window_options_are_ordered_and_include_72_hours() -> None:
    assert DEFAULT_RECENT_WINDOW == "72 hours"
    assert list(RECENT_WINDOW_OPTIONS) == ["24 hours", "72 hours", "7 days", "30 days"]
    assert RECENT_WINDOW_OPTIONS["72 hours"] == timedelta(hours=72)


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
    assert row["Discovery Type"] == "Verified recent publication"
    assert row["Evidence Age"] == "Published 2h ago"
    assert row["Published At"] == published_at
    assert row["First Seen"] == FIXED_NOW


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

    assert list(row)[:4] == ["Company", "Job Title", "Published At", "Application URL"]
    assert list(row)[-1] == "Job ID"


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

