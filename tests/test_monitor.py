from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ats_finder import cli
from ats_finder.config import Settings
from ats_finder.db import Database, SCHEMA_VERSION, datetime_to_storage
from ats_finder.models import SourceInput, SourceSupportStatus
from ats_finder.monitor import (
    MonitorRecorder,
    collect_snapshot,
    monitoring_session,
    read_samples,
    render_history,
    render_text,
    start_monitor_recorder,
)


UTC = timezone.utc
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _source(provider: str, board: str, status: SourceSupportStatus) -> SourceInput:
    return SourceInput(
        provider=provider,
        company=board.title(),
        board_identifier=board,
        career_page_url=f"https://example.com/{board}",
        support_status=status,
    )


def _build_database(path: Path) -> Database:
    database = Database(path)
    database.bulk_upsert_sources(
        [
            _source("ashby", "acme", SourceSupportStatus.SUPPORTED),
            _source("greenhouse", "beta", SourceSupportStatus.SUPPORTED),
            _source("lever", "gamma", SourceSupportStatus.SUPPORTED),
            _source("workable", "delta", SourceSupportStatus.UNSUPPORTED),
        ]
    )
    with database.transaction() as connection:
        connection.executemany(
            """
            INSERT INTO scan_runs (
                source_id, started_at, ended_at, completion_status,
                pagination_complete, record_count, error_information
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    datetime_to_storage(NOW - timedelta(minutes=30)),
                    datetime_to_storage(NOW - timedelta(minutes=29)),
                    "SUCCESS",
                    1,
                    5,
                    None,
                ),
                (
                    1,
                    datetime_to_storage(NOW - timedelta(minutes=20)),
                    datetime_to_storage(NOW - timedelta(minutes=19)),
                    "FAILED",
                    0,
                    0,
                    "HTTPStatusError: Client error '404 Not Found' for url "
                    "'https://api.ashbyhq.com/posting-api/job-board/acme'",
                ),
                (
                    2,
                    datetime_to_storage(NOW - timedelta(minutes=28)),
                    datetime_to_storage(NOW - timedelta(minutes=27)),
                    "SUCCESS",
                    1,
                    0,
                    None,
                ),
            ],
        )
        connection.execute(
            """
            UPDATE ats_sources
            SET last_scan_result = 'FAILED',
                last_attempted_scan_at = ?,
                next_scan_at = ?
            WHERE id = 1
            """,
            (
                datetime_to_storage(NOW - timedelta(minutes=19)),
                datetime_to_storage(NOW + timedelta(hours=1)),
            ),
        )
        connection.execute(
            """
            UPDATE ats_sources
            SET last_scan_result = 'SUCCESS',
                last_attempted_scan_at = ?,
                last_successful_scan_at = ?,
                next_scan_at = ?
            WHERE id = 2
            """,
            (
                datetime_to_storage(NOW - timedelta(minutes=27)),
                datetime_to_storage(NOW - timedelta(minutes=27)),
                datetime_to_storage(NOW + timedelta(hours=1)),
            ),
        )
    return database


def test_snapshot_reports_progress_rates_and_failures(tmp_path: Path) -> None:
    database = _build_database(tmp_path / "monitor.sqlite3")
    try:
        snapshot = collect_snapshot(
            database.path, window_minutes=60, stall_seconds=300, now=NOW
        )
    finally:
        database.close()

    assert snapshot.schema_version == SCHEMA_VERSION
    assert snapshot.supported_total == 3
    assert snapshot.supported_attempted == 2
    assert snapshot.supported_remaining == 1
    assert snapshot.scan_runs == 3
    assert snapshot.scan_runs_success == 2
    assert snapshot.scan_runs_failed == 1
    assert snapshot.recent_runs == 3
    assert snapshot.recent_boards_per_minute == pytest.approx(3 / 60)
    assert snapshot.eta_minutes == pytest.approx(20.0)
    assert snapshot.empty_success_scans == 1
    assert snapshot.last_scan_ended_at == NOW - timedelta(minutes=19)
    assert snapshot.seconds_since_last_scan == pytest.approx(19 * 60)
    assert snapshot.stalled is True

    providers = {item.provider: item for item in snapshot.providers}
    assert set(providers) == {"ashby", "greenhouse", "lever"}
    assert providers["ashby"].failed == 1
    assert providers["ashby"].succeeded == 0
    assert providers["greenhouse"].succeeded == 1
    assert providers["lever"].never_attempted == 1
    assert providers["lever"].eta_minutes is None

    assert snapshot.failure_reasons
    top = snapshot.failure_reasons[0]
    assert top.reason == "HTTP 404"
    assert top.count == 1
    assert top.by_provider == (("ashby", 1),)




def test_snapshot_is_read_only_and_renderable(tmp_path: Path) -> None:
    database = _build_database(tmp_path / "monitor.sqlite3")
    try:
        before = database.path
        snapshot = collect_snapshot(before, now=NOW)
        text = render_text(snapshot)
        with database.read() as connection:
            runs = connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    finally:
        database.close()

    assert runs == 3
    assert "ATS Finder monitor" in text
    assert "progress" in text
    assert "HTTP 404" in text
    assert "WARNING" in text
    assert "ashby" in text


def test_snapshot_rejects_missing_database_and_bad_window(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        collect_snapshot(tmp_path / "missing.sqlite3", now=NOW)
    with pytest.raises(ValueError):
        collect_snapshot(tmp_path / "missing.sqlite3", window_minutes=0, now=NOW)


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_monitoring_session_writes_start_and_stop_samples(tmp_path: Path) -> None:
    database = _build_database(tmp_path / "monitor.sqlite3")
    log_path = tmp_path / "telemetry" / "samples.jsonl"
    settings = Settings(
        database_path=Path(database.path),
        monitor_log_path=log_path,
        monitor_interval_seconds=3600,
    )
    try:
        with monitoring_session(settings) as recorder:
            assert isinstance(recorder, MonitorRecorder)
    finally:
        database.close()

    rows = _read_events(log_path)
    assert [row["event"] for row in rows] == ["start", "stop"]
    assert rows[0]["run_id"] == rows[1]["run_id"]
    assert rows[0]["pid"] == rows[1]["pid"]
    assert rows[0]["supported_total"] == 3
    assert rows[0]["supported_attempted"] == 2
    assert "captured_at" in rows[0]


def test_recorder_periodically_appends_samples(tmp_path: Path) -> None:
    database = _build_database(tmp_path / "monitor.sqlite3")
    recorder = MonitorRecorder(
        database.path,
        tmp_path / "samples.jsonl",
        interval_seconds=0.05,
        logger=logging.getLogger("test.monitor"),
    )
    try:
        recorder.start()
        deadline = time.monotonic() + 5.0
        while recorder.samples < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        recorder.stop()
    finally:
        database.close()

    assert recorder.samples >= 3
    events = [row["event"] for row in _read_events(recorder.log_path)]
    assert events[0] == "start"
    assert "sample" in events
    assert events[-1] == "stop"


def test_recorder_rotates_oversized_log(tmp_path: Path) -> None:
    database = _build_database(tmp_path / "monitor.sqlite3")
    recorder = MonitorRecorder(
        database.path,
        tmp_path / "samples.jsonl",
        interval_seconds=3600,
        max_bytes=16,
    )
    try:
        assert recorder.sample_now("start") is True
        assert recorder.sample_now("sample") is True
    finally:
        database.close()

    backup = tmp_path / "samples.jsonl.1"
    assert backup.exists()
    assert recorder.samples == 2
    assert len(_read_events(tmp_path / "samples.jsonl")) == 1


def test_recorder_is_disabled_without_log_path(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "demo.sqlite3", monitor_log_path=None)
    assert start_monitor_recorder(settings) is None


def test_recorder_survives_unavailable_database(tmp_path: Path) -> None:
    recorder = MonitorRecorder(
        tmp_path / "missing.sqlite3",
        tmp_path / "samples.jsonl",
        interval_seconds=3600,
        logger=logging.getLogger("test.monitor"),
    )
    recorder.start()
    recorder.stop()

    assert recorder.samples == 0
    assert recorder.failures == 2
    assert not (tmp_path / "samples.jsonl").exists()


def test_settings_derive_monitor_log_from_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATS_FINDER_MONITOR_LOG", raising=False)
    monkeypatch.setenv("ATS_FINDER_DB", str(tmp_path / "jobs.sqlite3"))
    settings = Settings.from_env()
    assert settings.monitor_log_path == tmp_path / "jobs.monitor.jsonl"
    assert settings.monitor_interval_seconds == 60

    monkeypatch.setenv("ATS_FINDER_MONITOR_LOG", "off")
    assert Settings.from_env().monitor_log_path is None

    custom = tmp_path / "elsewhere" / "scan.jsonl"
    monkeypatch.setenv("ATS_FINDER_MONITOR_LOG", str(custom))
    assert Settings.from_env().monitor_log_path == custom



def test_history_summarizes_saved_runs(tmp_path: Path) -> None:
    database = _build_database(tmp_path / "monitor.sqlite3")
    log_path = tmp_path / "samples.jsonl"
    settings = Settings(
        database_path=Path(database.path),
        monitor_log_path=log_path,
        monitor_interval_seconds=3600,
    )
    try:
        with monitoring_session(settings):
            pass
        with monitoring_session(settings):
            pass
    finally:
        database.close()

    rows = read_samples(log_path)
    assert len(rows) == 4
    text = render_history(log_path)
    assert "2 run(s)" in text
    assert "boards scanned +0" in text
    assert "Latest samples:" in text
    assert text.count("stop") >= 2


def test_history_rejects_missing_log(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_samples(tmp_path / "missing.jsonl")
    with pytest.raises(FileNotFoundError):
        render_history(tmp_path / "missing.jsonl")



def test_cli_monitor_writes_jsonl_and_reads_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _build_database(tmp_path / "monitor.sqlite3")
    log_path = tmp_path / "cli.jsonl"
    try:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "ats-finder",
                "--database",
                str(database.path),
                "monitor",
                "--jsonl",
                str(log_path),
            ],
        )
        cli.main()
        assert "ATS Finder monitor" in capsys.readouterr().out

        rows = read_samples(log_path)
        assert [row["event"] for row in rows] == ["start"]
        assert rows[0]["run_id"]

        monkeypatch.setattr(
            sys, "argv", ["ats-finder", "monitor", "--history", str(log_path)]
        )
        cli.main()
        history = capsys.readouterr().out
        assert "1 run(s)" in history
        assert "Latest samples:" in history
    finally:
        database.close()


def test_history_flags_runs_without_a_stop_sample(tmp_path: Path) -> None:
    log_path = tmp_path / "partial.jsonl"
    rows = [
        {
            "event": "start",
            "run_id": "r1",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "supported_attempted": 5,
        },
        {
            "event": "sample",
            "run_id": "r1",
            "captured_at": "2026-01-01T00:01:00+00:00",
            "supported_attempted": 9,
            "stalled": True,
        },
    ]
    log_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    text = render_history(log_path)
    assert "boards scanned +4" in text
    assert "stalled samples 1" in text
    assert "no stop sample (still running or killed)" in text


def test_snapshot_excludes_operator_excluded_boards(tmp_path: Path) -> None:
    path = tmp_path / "excluded-monitor.sqlite3"
    database = Database(path)
    try:
        database.bulk_upsert_sources(
            [
                _source("ashby", "acme", SourceSupportStatus.SUPPORTED),
                _source("ashby", "beta", SourceSupportStatus.SUPPORTED),
            ]
        )
        database.add_source_exclusion("ashby", "beta", "not suitable")
    finally:
        database.close()

    snapshot = collect_snapshot(path)

    assert snapshot.supported_total == 1
    assert snapshot.supported_attempted == 0
    assert snapshot.supported_remaining == 1


