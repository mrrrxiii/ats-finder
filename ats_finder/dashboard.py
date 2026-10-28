from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

import pandas as pd
import streamlit as st

from ats_finder.config import Settings
from ats_finder.db import Database, datetime_from_storage
from ats_finder.engine import ScanEngine
from ats_finder.models import (
    ApplicationStatus,
    DiscoveryType,
    EnginePolicy,
    RecentJob,
    SourceLastScanResult,
    SourceSupportStatus,
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
        return f"Published {_age(job.published_at, current)}"
    if job.discovery_type is DiscoveryType.NEWLY_DETECTED:
        return f"First detected {_age(job.detection_at, current)}"
    return discovery_label(job.discovery_type)


def recent_job_row(job: RecentJob, *, now: datetime | None = None) -> dict[str, object]:
    return {
        "Company": job.company,
        "Job Title": job.title,
        "Location": job.location,
        "Work Mode": job.workplace_type.value.title(),
        "Discovery Type": discovery_label(job.discovery_type),
        "Evidence Age": evidence_age_label(job, now=now),
        "Published At": job.published_at,
        "First Seen": job.first_seen_at,
        "Previous Board Scan": job.previous_board_scan_at,
        "ATS": job.provider.title(),
        "Application URL": job.application_url,
        "Application Status": job.application_status.value,
        "Job ID": job.job_id,
    }


def _frame(rows: Iterable[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def _render_job_table(frame: pd.DataFrame, *, key: str) -> None:
    if frame.empty:
        st.info("No jobs meet this section's confirmed evidence rules.")
        return
    visible = frame.drop(columns=["Job ID"], errors="ignore")
    st.dataframe(
        visible,
        width="stretch",
        hide_index=True,
        column_config={
            "Application URL": st.column_config.LinkColumn(
                "Official application", display_text="Apply"
            ),
            "Published At": st.column_config.DatetimeColumn(
                "Published At (verified)", format="YYYY-MM-DD HH:mm [UTC]"
            ),
            "First Seen": st.column_config.DatetimeColumn(
                "First Seen", format="YYYY-MM-DD HH:mm [UTC]"
            ),
            "Previous Board Scan": st.column_config.DatetimeColumn(
                "Previous Board Scan", format="YYYY-MM-DD HH:mm [UTC]"
            ),
        },
        key=f"table_{key}",
    )
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
    st.header("Latest Java Openings")
    st.caption(
        "Only confirmed Java roles in Ottawa or explicitly remote from Canada are shown. "
        "Publication time and first-detection time are deliberately not conflated."
    )
    unified = st.toggle("Use unified evidence-time sort", value=False)
    now = datetime.now(timezone.utc)
    jobs = ScanEngine(database).recent_jobs(now=now, unified_sort=unified)
    rows = [recent_job_row(job, now=now) for job in jobs]
    frame = _frame(rows)

    verified = frame[
        frame.get("Discovery Type", pd.Series(dtype=str))
        == discovery_label(DiscoveryType.VERIFIED_RECENT)
    ] if not frame.empty else frame
    detected = frame[
        frame.get("Discovery Type", pd.Series(dtype=str))
        == discovery_label(DiscoveryType.NEWLY_DETECTED)
    ] if not frame.empty else frame

    st.subheader("Verified recent publication")
    st.caption("The ATS exposes a documented publication or republication timestamp.")
    _render_job_table(verified, key="verified_recent_java_jobs")

    st.subheader("Newly detected by snapshot comparison")
    st.caption(
        "These jobs were absent from the preceding complete board snapshot, taken no "
        "more than 24 hours earlier. This is observation evidence, not a publication date."
    )
    _render_job_table(detected, key="newly_detected_java_jobs")

    if jobs:
        st.subheader("Job detail")
        selected = st.selectbox(
            "Choose a job",
            jobs,
            format_func=lambda job: f"{job.company} — {job.title}",
        )
        record = database.get_job(selected.job_id)
        if record is not None:
            st.markdown(f"### {record.title}")
            st.write(record.description or "No description was returned by the ATS.")
            st.link_button("Open official application", record.application_url)
            _application_editor(database, record.id, record.application_status)


def _coverage_rows(database: Database, now: datetime, stale_after: timedelta) -> pd.DataFrame:
    with database.read() as connection:
        rows = connection.execute(
            """
            SELECT s.*,
                   COUNT(CASE WHEN r.completion_status = 'SUCCESS' THEN 1 END) AS successful_runs,
                   MIN(CASE WHEN r.completion_status = 'SUCCESS' THEN r.ended_at END) AS first_success,
                   MAX(CASE WHEN r.completion_status = 'SUCCESS' THEN r.ended_at END) AS latest_success
            FROM ats_sources AS s
            LEFT JOIN scan_runs AS r ON r.source_id = s.id
            GROUP BY s.id
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
    tabs = st.tabs(["Supported", "Unsupported", "Failed", "Incomplete", "Stale"])
    filters = [
        frame[frame["Support"] == SourceSupportStatus.SUPPORTED.value],
        frame[frame["Support"] == SourceSupportStatus.UNSUPPORTED.value],
        frame[frame["Latest Result"] == SourceLastScanResult.FAILED.value],
        frame[frame["Latest Result"] == SourceLastScanResult.INCOMPLETE.value],
        frame[frame["Stale"] == True],  # noqa: E712 - pandas vector comparison
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


def _history_frame(database: Database) -> pd.DataFrame:
    with database.read() as connection:
        rows = connection.execute(
            """
            SELECT j.*, s.provider, s.board_identifier
            FROM jobs AS j
            JOIN ats_sources AS s ON s.id = j.source_id
            ORDER BY j.last_seen_at DESC, j.id DESC
            """
        ).fetchall()
    return _frame(
        {
            "Job ID": int(row["id"]),
            "Company": row["company"],
            "Job Title": row["title"],
            "Location": row["location"],
            "ATS": str(row["provider"]).title(),
            "First Seen": datetime_from_storage(row["first_seen_at"]),
            "Last Seen": datetime_from_storage(row["last_seen_at"]),
            "State": row["current_state"],
            "Discovery": discovery_label(row["discovery_type"]),
            "Java": bool(row["java_keyword_match"]),
            "Location Decision": row["location_match"],
            "Potential Repost Of": row["potential_repost_of_job_id"],
            "Application Status": row["application_status"],
            "Application URL": row["application_url"],
        }
        for row in rows
    )


def history_page(database: Database, settings: Settings) -> None:
    del settings
    st.header("Job History")
    frame = _history_frame(database)
    if frame.empty:
        st.info("No job history exists yet. Complete a board scan to create a baseline.")
        return
    search = st.text_input("Search company, title, or location")
    state = st.selectbox("Presence", ["ALL", "ACTIVE", "REMOVED"])
    filtered = frame
    if search:
        mask = (
            filtered[["Company", "Job Title", "Location"]]
            .fillna("")
            .astype(str)
            .apply(lambda column: column.str.contains(search, case=False, regex=False))
            .any(axis=1)
        )
        filtered = filtered[mask]
    if state != "ALL":
        filtered = filtered[filtered["State"] == state]

    st.dataframe(
        filtered.drop(columns=["Job ID"]),
        width="stretch",
        hide_index=True,
        column_config={
            "Application URL": st.column_config.LinkColumn(
                "Official application", display_text="Apply"
            )
        },
    )
    st.download_button(
        "Download filtered history CSV",
        filtered.to_csv(index=False).encode("utf-8"),
        file_name="java_job_history.csv",
        mime="text/csv",
    )

    available_ids = [int(value) for value in filtered["Job ID"].tolist()]
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
    st.link_button("Open official application", job.application_url)
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
            "Application URL": item["application_url"],
        }
        for item in versions
    )
    st.subheader("Immutable content versions")
    st.dataframe(
        version_frame,
        width="stretch",
        hide_index=True,
        column_config={
            "Application URL": st.column_config.LinkColumn(
                "Official application", display_text="Apply"
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

