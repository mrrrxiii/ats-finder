from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

import pandas as pd
import streamlit as st

from ats_finder.config import Settings
from ats_finder.db import Database, datetime_from_storage
from ats_finder.engine import ScanEngine
from ats_finder.models import (
    ApplicationStatus,
    DiscoveryType,
    EnginePolicy,
    PublicationSemantics,
    RecentJob,
    SourceLastScanResult,
    SourceSupportStatus,
)
from ats_finder.urls import job_detail_url


RECENT_WINDOW_OPTIONS: dict[str, timedelta] = {
    "24 hours": timedelta(hours=24),
    "72 hours": timedelta(hours=72),
    "7 days": timedelta(days=7),
    "30 days": timedelta(days=30),
}
DEFAULT_RECENT_WINDOW = "72 hours"

JOB_DETAIL_SELECTION_KEY = "job_detail_selection"
ROW_PICK_STATE_KEY = "job_detail_row_pick"

_FRESHNESS_STYLES = (
    (
        "New posting",
        "background-color: #dcfce7; color: #166534; font-weight: 600;",
    ),
    (
        "Reposted",
        "background-color: #fef3c7; color: #92400e; font-weight: 600;",
    ),
    (
        "New to this site · timing uncertain",
        "background-color: #ffedd5; color: #9a3412; font-weight: 600;",
    ),
    (
        "Relisted",
        "background-color: #ede9fe; color: #5b21b6; font-weight: 600;",
    ),
    (
        "New to this site",
        "background-color: #dbeafe; color: #1e40af; font-weight: 600;",
    ),
    (
        "First seen here",
        "background-color: #f3f4f6; color: #374151; font-weight: 600;",
    ),
)


DISCOVERY_LABELS = {
    DiscoveryType.BASELINE: "Baseline (publication unknown)",
    DiscoveryType.VERIFIED_RECENT: "Verified recent publication",
    DiscoveryType.VERIFIED_OLD: "Verified publication older than 24 hours",
    DiscoveryType.VERIFIED_TIME_INVALID: "Publication time invalid",
    DiscoveryType.NEWLY_DETECTED: "Newly detected by snapshot comparison",
    DiscoveryType.NEWLY_DETECTED_TIME_UNCERTAIN: "Newly detected; timing uncertain",
    DiscoveryType.REAPPEARED: "Reappeared",
    DiscoveryType.POTENTIAL_REPOST: "Potential repost / duplicate",
}


def discovery_label(value: DiscoveryType | str) -> str:
    return DISCOVERY_LABELS[DiscoveryType(value)]


def _age(moment: datetime, now: datetime) -> str:
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def evidence_age_label(job: RecentJob, *, now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if job.discovery_type is DiscoveryType.VERIFIED_RECENT:
        if job.published_at is None:
            raise ValueError("VERIFIED_RECENT job is missing its publication timestamp")
        action = (
            "Republished"
            if job.publication_semantics is PublicationSemantics.REPUBLICATION
            else "Published"
        )
        return f"{action} {_age(job.published_at, current)}"
    if job.discovery_type in {
        DiscoveryType.BASELINE,
        DiscoveryType.NEWLY_DETECTED,
        DiscoveryType.NEWLY_DETECTED_TIME_UNCERTAIN,
        DiscoveryType.REAPPEARED,
    }:
        return f"First detected {_age(job.detection_at, current)}"
    return discovery_label(job.discovery_type)


def posting_status_label(job: RecentJob) -> str:
    if (
        job.publication_semantics is PublicationSemantics.REPUBLICATION
        and job.published_at is not None
        and job.published_at > job.first_seen_at
    ):
        return "Reposted"
    if job.discovery_type is DiscoveryType.REAPPEARED:
        return "Relisted"
    if job.discovery_type is DiscoveryType.VERIFIED_RECENT:
        return "New posting"
    if job.discovery_type is DiscoveryType.NEWLY_DETECTED:
        return "New to this site"
    if job.discovery_type is DiscoveryType.NEWLY_DETECTED_TIME_UNCERTAIN:
        return "New to this site · timing uncertain"
    if job.discovery_type is DiscoveryType.BASELINE:
        return "First seen here"
    return discovery_label(job.discovery_type)


def freshness_label(job: RecentJob, *, now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    status = posting_status_label(job)
    if job.discovery_type is DiscoveryType.VERIFIED_RECENT:
        if job.published_at is None:
            raise ValueError("VERIFIED_RECENT job is missing its publication timestamp")
        return f"{status} · {_age(job.published_at, current)}"
    return f"{status} · {_age(job.detection_at, current)}"


def freshness_cell_style(value: object) -> str:
    text = str(value)
    for prefix, style in sorted(
        _FRESHNESS_STYLES,
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if text.startswith(prefix):
            return style
    return ""


def recent_job_row(job: RecentJob, *, now: datetime | None = None) -> dict[str, object]:
    return {
        "Company": job.company,
        "Job Title": job.title,
        "Published At": job.published_at,
        "Job URL": job_detail_url(job.application_url),
        "Location": job.location,
        "Work Mode": job.workplace_type.value.title(),
        "Status": freshness_label(job, now=now),
        "First Detected": job.first_seen_at,
        "Application Status": job.application_status.value,
        "_Discovery Type": job.discovery_type.value,
        "Job ID": job.job_id,
    }


def _frame(rows: Iterable[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def row_pick_job_id(frame: pd.DataFrame, rows: Iterable[int]) -> int | None:
    """Return the job id behind a clicked table row, or None when it cannot be mapped."""
    if "Job ID" not in frame.columns:
        return None
    picked = list(rows)
    if not picked:
        return None
    index = picked[0]
    if index < 0 or index >= len(frame):
        return None
    return int(frame["Job ID"].iloc[index])


def _render_job_table(
    frame: pd.DataFrame,
    *,
    key: str,
    on_row_pick: Callable[[int], None] | None = None,
) -> None:
    if frame.empty:
        st.info("No jobs meet this section's confirmed evidence rules.")
        return
    visible = frame.drop(
        columns=["Job ID", "_Discovery Type"],
        errors="ignore",
    )
    rendered = (
        visible.style.map(freshness_cell_style, subset=["Status"])
        if "Status" in visible.columns
        else visible
    )
    event = st.dataframe(
        rendered,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Job URL": st.column_config.LinkColumn(
                "Job description", display_text="View job"
            ),
            "Published At": st.column_config.DatetimeColumn(
                "Published At (verified)", format="YYYY-MM-DD HH:mm [UTC]"
            ),
            "First Detected": st.column_config.DatetimeColumn(
                "First detected", format="YYYY-MM-DD HH:mm [UTC]"
            ),
        },
        key=f"table_{key}",
    )
    if on_row_pick is not None and event is not None:
        job_id = row_pick_job_id(frame, event.selection.rows)
        if job_id is not None:
            signature = f"{key}:{job_id}"
            if st.session_state.get(ROW_PICK_STATE_KEY) != signature:
                st.session_state[ROW_PICK_STATE_KEY] = signature
                on_row_pick(job_id)
    st.download_button(
        "Download CSV",
        visible.to_csv(index=False).encode("utf-8"),
        file_name=f"{key}.csv",
        mime="text/csv",
        key=f"csv_{key}",
    )


def _application_editor(database: Database, job_id: int, current: ApplicationStatus) -> None:
    values = list(ApplicationStatus)
    selected = st.selectbox(
        "Application status",
        values,
        index=values.index(current),
        format_func=lambda item: item.value.replace("_", " ").title(),
        key=f"application_status_{job_id}",
    )
    if st.button("Save application status", key=f"save_application_{job_id}"):
        ScanEngine(database).set_application_status(job_id, selected)
        st.success("Application status saved.")


def latest_page(database: Database, settings: Settings) -> None:
    del settings
    st.header("Latest Java Openings")
    st.caption(
        "Only confirmed Java roles in Ottawa or explicitly remote from Canada are shown. "
        "Titles containing Staff, Principle, Principal, Manager, or Architect are excluded. "
        "Verified publication time and first-observation time are deliberately not conflated."
    )
    window_label = st.selectbox(
        "Evidence window",
        list(RECENT_WINDOW_OPTIONS),
        index=list(RECENT_WINDOW_OPTIONS).index(DEFAULT_RECENT_WINDOW),
        key="recent_window",
        help=(
            "Verified publication or first-observation time must fall inside this "
            "window to be listed."
        ),
    )
    window = RECENT_WINDOW_OPTIONS[window_label]
    unified = st.toggle("Use unified evidence-time sort", value=False)
    now = datetime.now(timezone.utc)
    engine = ScanEngine(database)
    jobs = engine.recent_jobs(now=now, window=window, unified_sort=unified)
    rows = [recent_job_row(job, now=now) for job in jobs]
    frame = _frame(rows)

    verified = frame[
        frame.get("_Discovery Type", pd.Series(dtype=str))
        == DiscoveryType.VERIFIED_RECENT.value
    ] if not frame.empty else frame
    detected = frame[
        frame.get("_Discovery Type", pd.Series(dtype=str))
        == DiscoveryType.NEWLY_DETECTED.value
    ] if not frame.empty else frame
    first_observed = frame[
        frame.get("_Discovery Type", pd.Series(dtype=str)).isin(
            [
                DiscoveryType.BASELINE.value,
                DiscoveryType.NEWLY_DETECTED_TIME_UNCERTAIN.value,
                DiscoveryType.REAPPEARED.value,
            ]
        )
    ] if not frame.empty else frame

    def _select_job(job_id: int) -> None:
        """Select a job for the detail panel without touching the picker mid-run."""
        if st.session_state.get(JOB_DETAIL_SELECTION_KEY) != str(job_id):
            st.session_state[JOB_DETAIL_SELECTION_KEY] = str(job_id)

    st.subheader("Posted dates from the ATS")
    st.caption(
        "The ATS exposes a documented publication or republication timestamp inside "
        f"the last {window_label}. A job is reposted only when its ATS "
        "publication timestamp advances after this site first detects it."
    )
    _render_job_table(
        verified, key="verified_recent_java_jobs", on_row_pick=_select_job
    )

    snapshot_gap_hours = engine.policy.snapshot_max_gap.total_seconds() / 3600
    st.subheader("New to this site")
    st.caption(
        "These jobs were absent from the preceding complete board snapshot. This "
        "means they are new to this site, not necessarily newly published by the "
        f"employer. Valid comparisons use a prior snapshot no more than "
        f"{snapshot_gap_hours:g} hours older."
    )
    _render_job_table(
        detected, key="newly_detected_java_jobs", on_row_pick=_select_job
    )

    st.subheader("First observed by this site")
    st.caption(
        "These jobs match the Java and location filters but have no verified "
        f"publication time. Their first observation is within the same "
        f"{window_label} evidence window selected above; it is not a publication "
        "date."
    )
    _render_job_table(
        first_observed, key="first_observed_java_jobs", on_row_pick=_select_job
    )

    with st.expander("What do job statuses mean?"):
        st.markdown(
            """
            - **New posting:** the job has a verified recent ATS publication
              timestamp and no observed later repost.
            - **Reposted:** the same stable job ID was detected earlier, and the
              ATS publication timestamp later advanced.
            - **New to this site:** the job appeared in a later complete snapshot,
              but no verified publication date is available.
            - **New to this site · timing uncertain:** the comparison gap was too
              old to establish reliable snapshot newness.
            - **Relisted:** a previously removed job ID appeared again.
            - **First seen here:** the job was present during the initial baseline
              scan and has no verified publication date.
            """
        )

    if jobs:
        st.subheader("Job detail")
        labels = {str(job.job_id): f"{job.company} — {job.title}" for job in jobs}
        options = list(labels)
        if st.session_state.get(JOB_DETAIL_SELECTION_KEY) not in labels:
            st.session_state[JOB_DETAIL_SELECTION_KEY] = options[0]
        selected_id = st.selectbox(
            "Choose a job",
            options,
            format_func=lambda option: labels.get(option, option),
            key=JOB_DETAIL_SELECTION_KEY,
            help="Click a table row or pick a job here; this panel keeps your choice "
            "while new results arrive.",
        )
        selected_job = next(
            job for job in jobs if job.job_id == int(selected_id)
        )
        record = database.get_job(int(selected_id))
        if record is not None:
            st.markdown(f"### {record.title}")
            if (
                posting_status_label(selected_job) == "Reposted"
                and selected_job.published_at is not None
            ):
                st.warning(
                    "This posting was published again after we first detected it. "
                    "It is not necessarily a new role. "
                    f"Latest ATS publication: {selected_job.published_at:%Y-%m-%d %H:%M UTC}; "
                    f"first detected: {selected_job.first_seen_at:%Y-%m-%d %H:%M UTC}."
                )
            st.write(record.description or "No description was returned by the ATS.")
            st.link_button("Open job description", job_detail_url(record.application_url))
            _application_editor(database, record.id, record.application_status)


def _coverage_rows(database: Database, now: datetime, stale_after: timedelta) -> pd.DataFrame:
    with database.read() as connection:
        rows = connection.execute(
            """
            WITH run_stats AS (
                SELECT
                    source_id,
                    COUNT(CASE WHEN completion_status = 'SUCCESS' THEN 1 END)
                        AS successful_runs,
                    MIN(CASE WHEN completion_status = 'SUCCESS' THEN ended_at END)
                        AS first_success,
                    MAX(CASE WHEN completion_status = 'SUCCESS' THEN ended_at END)
                        AS latest_success
                FROM scan_runs
                GROUP BY source_id
            )
            SELECT s.*,
                   EXISTS (
                       SELECT 1 FROM source_exclusions AS e
                       WHERE e.provider = s.provider
                         AND e.board_identifier = s.board_identifier
                   ) AS is_excluded,
                   COALESCE(r.successful_runs, 0) AS successful_runs,
                   r.first_success,
                   r.latest_success
            FROM ats_sources AS s
            LEFT JOIN run_stats AS r ON r.source_id = s.id
            ORDER BY s.company COLLATE NOCASE, s.id
            """
        ).fetchall()
    rendered: list[dict[str, object]] = []
    for row in rows:
        first = datetime_from_storage(row["first_success"])
        latest = datetime_from_storage(row["latest_success"])
        successes = int(row["successful_runs"] or 0)
        average_hours: float | None = None
        if successes > 1 and first is not None and latest is not None:
            average_hours = (latest - first).total_seconds() / 3600 / (successes - 1)
        rendered.append(
            {
                "Source ID": int(row["id"]),
                "Company": row["company"],
                "ATS": str(row["provider"]).title(),
                "Support": row["support_status"],
                "Excluded": bool(row["is_excluded"]),
                "Canada Presence": row["canada_presence"],
                "Latest Result": row["last_scan_result"],
                "Last Successful Scan": latest,
                "Successful Scans": successes,
                "Average Success Interval (hours)": average_hours,
                "Stale": latest is not None and latest < now - stale_after,
                "Failure Count": int(row["consecutive_failures"]),
                "Career Page": row["career_page_url"],
                "Support Reason": row["support_reason"],
            }
        )
    return _frame(rendered)


@dataclass(frozen=True)
class CoverageProgress:
    """Scan completeness of the supported board set, plus its current freshness."""

    supported: int
    covered: int
    attempted: int
    failing: int
    fresh: int

    @property
    def coverage_ratio(self) -> float:
        """Share of supported boards with at least one successful scan."""

        if not self.supported:
            return 0.0
        return self.covered / self.supported

    @property
    def fresh_ratio(self) -> float:
        """Share of supported boards whose last success is inside the stale window."""

        if not self.supported:
            return 0.0
        return self.fresh / self.supported


def coverage_progress(
    frame: pd.DataFrame, *, now: datetime, stale_after: timedelta
) -> CoverageProgress:
    """Measure how much of the supported board set is scanned and still current.

    Unsupported ATS types, operator-excluded boards, and boards deferred by the
    no-Canada policy are excluded from every count: they can never, must never,
    or are intentionally not being fetched at the normal interval, so counting
    them would cap the percentage below 100% permanently.
    A board counts as covered once one scan succeeded, and stays fresh only
    while that success is newer than the stale window, which is why coverage
    rises monotonically while freshness falls whenever scanning lags.
    """

    if frame.empty:
        return CoverageProgress(supported=0, covered=0, attempted=0, failing=0, fresh=0)
    supported = frame[
        (frame["Support"] == SourceSupportStatus.SUPPORTED.value)
        & (~frame["Excluded"].astype(bool))
        & (frame["Canada Presence"] != "NONE")
    ]
    latest_success = supported["Last Successful Scan"]
    results = supported["Latest Result"]
    cutoff = now - stale_after
    fresh = sum(
        1
        for value in latest_success
        if isinstance(value, datetime) and value >= cutoff
    )
    return CoverageProgress(
        supported=len(supported),
        covered=int(latest_success.notna().sum()),
        attempted=int((results != SourceLastScanResult.NOT_SCANNED.value).sum()),
        failing=int((results == SourceLastScanResult.FAILED.value).sum()),
        fresh=fresh,
    )


def coverage_page(database: Database, settings: Settings) -> None:
    st.header("Scan Coverage")
    now = datetime.now(timezone.utc)
    stale_after = timedelta(seconds=settings.stale_after_seconds)
    policy = EnginePolicy(stale_source_after=stale_after)
    stats = ScanEngine(database, policy).coverage_stats(now=now)
    first = st.columns(5)
    first[0].metric("Imported boards", stats.imported_sources)
    first[1].metric("Successfully scanned", stats.successfully_scanned_sources)
    first[2].metric("Failed", stats.failed_sources)
    first[3].metric("Incomplete", stats.incomplete_sources)
    first[4].metric("Not yet scanned", stats.not_yet_scanned_sources)
    second = st.columns(5)
    second[0].metric("Stale snapshots", stats.stale_sources or 0)
    second[1].metric("Unique jobs inspected", stats.total_jobs_inspected)
    second[2].metric("Verified timestamps", stats.jobs_with_verified_publication_timestamps)
    second[3].metric("Snapshot discoveries", stats.newly_detected_jobs)
    second[4].metric("Ambiguous locations", stats.ambiguous_location_jobs)
    st.metric("Unknown dates with insufficient history", stats.insufficient_history_jobs)
    st.caption(
        f"A source is stale after {settings.stale_after_intervals:g} configured scan intervals "
        f"({settings.stale_after_seconds / 3600:g} hours). Never-scanned sources are separate."
    )

    frame = _coverage_rows(database, now, stale_after)
    if frame.empty:
        st.info("No ATS sources have been imported yet.")
        return

    progress = coverage_progress(frame, now=now, stale_after=stale_after)
    st.subheader("Scan progress")
    if not progress.supported:
        st.info(
            "No supported ATS boards have been imported, so there is no progress "
            "to measure. Unsupported boards are listed in their own tab."
        )
    else:
        stale_hours = settings.stale_after_seconds / 3600
        columns = st.columns(3)
        columns[0].metric(
            "Coverage",
            f"{progress.coverage_ratio:.1%}",
            help=(
                "Supported boards scanned successfully at least once, divided by "
                "supported boards that are neither operator-excluded nor deferred by "
                "the no-Canada policy. This cannot decrease and reaches 100% when "
                "the first full pass finishes."
            ),
        )
        columns[1].metric(
            "Supported boards covered",
            f"{progress.covered:,} of {progress.supported:,}",
        )
        columns[2].metric(
            "Fresh within stale window",
            f"{progress.fresh_ratio:.1%}",
            help=(
                f"Supported boards whose last success is newer than {stale_hours:g} "
                "hours. This falls when scanning cannot keep up with the interval, "
                "so it reports how much of the coverage is usable right now."
            ),
        )
        st.progress(
            progress.coverage_ratio,
            text=(
                f"{progress.covered:,} of {progress.supported:,} eligible supported "
                "boards scanned successfully at least once"
            ),
        )
        st.caption(
            f"{progress.attempted:,} of {progress.supported:,} eligible supported "
            f"boards have been attempted and {progress.failing:,} currently report "
            "FAILED. Unsupported ATS types, operator-excluded boards, and no-Canada "
            "boards deferred to a monthly recheck are excluded from all three figures "
            "because they cannot or should not be fetched at the normal interval."
        )

    excluded = frame["Excluded"].astype(bool)
    no_canada = frame["Canada Presence"] == "NONE"
    tabs = st.tabs(
        [
            "Supported",
            "Unsupported",
            "Excluded",
            "No Canada",
            "Failed",
            "Incomplete",
            "Stale",
        ]
    )
    filters = [
        frame[(frame["Support"] == SourceSupportStatus.SUPPORTED.value) & ~excluded],
        frame[(frame["Support"] == SourceSupportStatus.UNSUPPORTED.value) & ~excluded],
        frame[excluded],
        frame[no_canada & ~excluded],
        frame[(frame["Latest Result"] == SourceLastScanResult.FAILED.value) & ~excluded],
        frame[
            (frame["Latest Result"] == SourceLastScanResult.INCOMPLETE.value)
            & ~excluded
        ],
        frame[(frame["Stale"] == True) & ~excluded],  # noqa: E712 - pandas vector comparison
    ]
    for tab, subset in zip(tabs, filters, strict=True):
        with tab:
            st.dataframe(
                subset,
                width="stretch",
                hide_index=True,
                column_config={
                    "Career Page": st.column_config.LinkColumn(
                        "Career Page", display_text="Open"
                    )
                },
            )


def _history_frame(
    database: Database,
    *,
    company: str = "",
    title: str = "",
    location: str = "",
    description: str = "",
    whole_word: bool = False,
    state: str = "ALL",
    limit: int = 2_000,
) -> pd.DataFrame:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if state not in {"ALL", "ACTIVE", "REMOVED"}:
        raise ValueError("state must be ALL, ACTIVE, or REMOVED")
    clauses: list[str] = []
    parameters: list[object] = []
    for column, value in (
        ("company", company),
        ("title", title),
        ("location", location),
    ):
        normalized_value = value.strip()
        if normalized_value:
            if whole_word:
                clauses.append(f"WHOLE_WORD(j.{column}, ?) = 1")
            else:
                clauses.append(f"INSTR(LOWER(j.{column}), LOWER(?)) > 0")
            parameters.append(normalized_value)
    normalized_description = description.strip()
    if normalized_description:
        for term in normalized_description.split():
            if whole_word:
                clauses.append(
                    "WHOLE_WORD(COALESCE(v.description, ''), ?) = 1"
                )
            else:
                clauses.append(
                    "INSTR(LOWER(COALESCE(v.description, '')), LOWER(?)) > 0"
                )
            parameters.append(term)
    if state != "ALL":
        clauses.append("j.current_state = ?")
        parameters.append(state)
    where = "" if not clauses else "WHERE " + " AND ".join(clauses)
    parameters.append(limit)
    with database.read() as connection:
        frame = pd.read_sql_query(
            f"""
            SELECT j.id, j.company, j.title, j.location, j.first_seen_at,
                   j.last_seen_at, j.current_state, j.discovery_type,
                   j.java_keyword_match, j.location_match,
                   j.potential_repost_of_job_id, j.application_status,
                   j.application_url
            FROM jobs AS j
            LEFT JOIN job_versions AS v
              ON v.job_id = j.id
             AND v.content_fingerprint = j.content_fingerprint
            {where}
            ORDER BY j.last_seen_at DESC, j.id DESC
            LIMIT ?
            """,
            connection,
            params=tuple(parameters),
        )
    frame["First Detected"] = pd.to_datetime(
        frame["first_seen_at"], utc=True, format="mixed"
    )
    frame["Last Seen"] = pd.to_datetime(frame["last_seen_at"], utc=True, format="mixed")
    frame["Discovery"] = frame["discovery_type"].map(discovery_label)
    frame["Java"] = frame["java_keyword_match"].astype(bool)
    frame["application_url"] = frame["application_url"].map(job_detail_url)
    return frame.rename(
        columns={
            "id": "Job ID",
            "company": "Company",
            "title": "Job Title",
            "location": "Location",
            "current_state": "State",
            "location_match": "Location Decision",
            "potential_repost_of_job_id": "Potential Repost Of",
            "application_status": "Application Status",
            "application_url": "Job URL",
        }
    )[
        [
            "Job ID",
            "Company",
            "Job Title",
            "Location",
            "Job URL",
            "First Detected",
            "Last Seen",
            "State",
            "Discovery",
            "Java",
            "Location Decision",
            "Potential Repost Of",
            "Application Status",
        ]
    ]


def history_page(database: Database, settings: Settings) -> None:
    del settings
    st.header("Job History")
    whole_word = st.toggle(
        "Whole-word search",
        value=False,
        help="Match complete words, so Java does not also match JavaScript.",
    )
    search_columns = st.columns(4)
    company = search_columns[0].text_input("Company")
    title = search_columns[1].text_input("Job title")
    location = search_columns[2].text_input("Location")
    description = search_columns[3].text_input("Job description")
    st.caption(
        "Multiple Job description words are combined with AND: "
        "`java kubernetes` requires both terms."
    )
    state = st.selectbox("Presence", ["ALL", "ACTIVE", "REMOVED"])
    limit = st.selectbox(
        "Rows to display",
        [500, 2_000, 5_000, 10_000],
        index=1,
        help="Search runs in SQLite before this limit is applied.",
    )
    frame = _history_frame(
        database,
        company=company,
        title=title,
        location=location,
        description=description,
        whole_word=whole_word,
        state=state,
        limit=limit,
    )
    if frame.empty:
        st.info(
            "No job history matches these filters. Clear the search fields or run "
            "a board scan to create history."
        )
        return

    st.dataframe(
        frame.drop(columns=["Job ID"]),
        width="stretch",
        hide_index=True,
        column_config={
            "Job URL": st.column_config.LinkColumn(
                "Job description", display_text="View job"
            )
        },
    )
    st.download_button(
        "Download displayed history CSV",
        frame.to_csv(index=False).encode("utf-8"),
        file_name="java_job_history_page.csv",
        mime="text/csv",
    )

    available_ids = [int(value) for value in frame["Job ID"].tolist()]
    if not available_ids:
        return
    selected_id = st.selectbox(
        "View job detail and change history",
        available_ids,
        format_func=lambda value: (
            f"{frame.loc[frame['Job ID'] == value, 'Company'].iloc[0]} — "
            f"{frame.loc[frame['Job ID'] == value, 'Job Title'].iloc[0]}"
        ),
    )
    job = database.get_job(selected_id)
    if job is None:
        return
    st.markdown(f"### {job.title}")
    st.write(job.description or "No description was returned by the ATS.")
    st.link_button("Open job description", job_detail_url(job.application_url))
    _application_editor(database, job.id, job.application_status)
    observations = database.list_observations(job_id=job.id)
    observation_frame = _frame(
        {
            "Observed At": item.observation_timestamp,
            "Event": item.observation_status.value,
            "Discovery": discovery_label(item.discovery_type),
            "Present": item.is_present,
            "Title": item.title,
            "Location": item.location,
            "Content Fingerprint": item.content_fingerprint,
        }
        for item in observations
    )
    st.subheader("Observation and change history")
    st.dataframe(observation_frame, width="stretch", hide_index=True)

    versions = database.list_job_versions(job_id=job.id)
    version_frame = _frame(
        {
            "Recorded At": datetime_from_storage(str(item["recorded_at"])),
            "Title": item["title"],
            "Location": item["location"],
            "Work Mode": str(item["workplace_type"]).title(),
            "Published At": datetime_from_storage(item["published_at"]),
            "Publication Evidence": item["publication_source"],
            "Timestamp Semantics": item["publication_semantics"],
            "Timestamp Verified": bool(item["publication_is_verified"]),
            "Content Fingerprint": item["content_fingerprint"],
            "Job URL": job_detail_url(str(item["application_url"])),
        }
        for item in versions
    )
    st.subheader("Immutable content versions")
    st.dataframe(
        version_frame,
        width="stretch",
        hide_index=True,
        column_config={
            "Job URL": st.column_config.LinkColumn(
                "Job description", display_text="View job"
            )
        },
    )

    status_history = database.list_application_status_history(job_id=job.id)
    status_frame = _frame(
        {
            "Recorded At": datetime_from_storage(str(item["recorded_at"])),
            "Previous Status": item["previous_status"],
            "New Status": item["new_status"],
            "Change Kind": item["change_kind"],
        }
        for item in status_history
    )
    st.subheader("Application status history")
    st.dataframe(status_frame, width="stretch", hide_index=True)


def main() -> None:
    settings = Settings.from_env()
    st.set_page_config(
        page_title="Canada Java ATS Finder",
        page_icon="🍁",
        layout="wide",
    )
    st.title("Canada Java ATS Finder")
    st.caption("Evidence-first monitoring of official public ATS boards")
    page = st.sidebar.radio(
        "Page",
        ["Latest Java Openings", "Scan Coverage", "Job History"],
    )
    with Database(settings.database_path) as database:
        if page == "Latest Java Openings":
            latest_page(database, settings)
        elif page == "Scan Coverage":
            coverage_page(database, settings)
        else:
            history_page(database, settings)


if __name__ == "__main__":
    main()

