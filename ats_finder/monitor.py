"""Read-only progress telemetry for long-running scans.

The monitor never writes to the database, never runs migrations, and never holds
a write lock, so it is safe to run in a second terminal while the worker or an
initial ``scan-due`` pass is committing board checkpoints.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote
import itertools
import json
import logging
import os
import re
import sqlite3
import threading

from ats_finder.config import Settings
from ats_finder.db import datetime_from_storage, datetime_to_storage
from ats_finder.models import as_utc, utc_now


LOGGER = logging.getLogger("ats_finder.monitor")
_RUN_SEQUENCE = itertools.count(1)


def new_run_id() -> str:
    """Unique, sortable identifier for one telemetry recording session."""

    return (
        f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
        f"-{next(_RUN_SEQUENCE)}"
    )


_HTTP_STATUS = re.compile(r"(?:Client|Server) error '(\d{3})")
_FAILURE_SAMPLE_LIMIT = 5000


@dataclass(frozen=True)
class ProviderProgress:
    provider: str
    total: int
    attempted: int
    never_attempted: int
    succeeded: int
    failed: int
    incomplete: int
    recent_runs: int
    recent_success: int
    recent_failed: int
    recent_boards_per_minute: float
    eta_minutes: float | None


@dataclass(frozen=True)
class FailureReason:
    reason: str
    count: int
    by_provider: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class MonitorSnapshot:
    captured_at: datetime
    database_path: str
    database_bytes: int
    wal_bytes: int
    schema_version: int | None
    supported_total: int
    supported_attempted: int
    supported_remaining: int
    scan_runs: int
    scan_runs_success: int
    scan_runs_failed: int
    scan_runs_incomplete: int
    first_scan_started_at: datetime | None
    last_scan_ended_at: datetime | None
    seconds_since_last_scan: float | None
    stalled: bool
    window_minutes: float
    recent_runs: int
    recent_success: int
    recent_failed: int
    overall_boards_per_minute: float
    recent_boards_per_minute: float
    eta_minutes: float | None
    providers: tuple[ProviderProgress, ...]
    failure_reasons: tuple[FailureReason, ...]
    empty_success_scans: int
    repeated_failure_sources: int
    jobs: int
    observations: int
    job_versions: int
    job_events: int
    repost_candidates: int
    java_matches: int
    confirmed_locations: int
    java_confirmed: int


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _failure_label(information: str | None) -> str:
    text = (information or "").strip()
    if not text:
        return "unknown"
    match = _HTTP_STATUS.search(text)
    if match is not None:
        return f"HTTP {int(match.group(1))}"
    head = text.split(":", 1)[0].strip()
    return head[:60] if head else "unknown"


def collect_snapshot(
    database_path: str | Path,
    *,
    window_minutes: float = 15.0,
    stall_seconds: float = 300.0,
    now: datetime | None = None,
) -> MonitorSnapshot:
    """Read one consistent progress sample without mutating the database."""

    if window_minutes <= 0:
        raise ValueError("window_minutes must be positive")
    if stall_seconds <= 0:
        raise ValueError("stall_seconds must be positive")
    current = as_utc(now) if now is not None else utc_now()
    target = Path(database_path).expanduser()
    if not target.exists():
        raise FileNotFoundError(f"database not found: {target}")
    target = target.resolve()
    since = datetime_to_storage(current - timedelta(minutes=window_minutes))

    connection = _connect_read_only(target)
    try:
        connection.execute("BEGIN")
        try:
            metadata = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
            schema_version = int(metadata["value"]) if metadata is not None else None

            totals = connection.execute(
                """
                SELECT COUNT(*) AS runs,
                       MIN(started_at) AS first_started,
                       MAX(ended_at) AS last_ended,
                       SUM(CASE WHEN completion_status = 'SUCCESS' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN completion_status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN completion_status = 'INCOMPLETE' THEN 1 ELSE 0 END) AS incomplete
                FROM scan_runs
                """
            ).fetchone()

            provider_rows = connection.execute(
                """
                SELECT provider,
                       COUNT(*) AS total,
                       SUM(CASE WHEN last_scan_result != 'NOT_SCANNED' THEN 1 ELSE 0 END) AS attempted,
                       SUM(CASE WHEN last_scan_result = 'SUCCESS' THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN last_scan_result = 'FAILED' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN last_scan_result = 'INCOMPLETE' THEN 1 ELSE 0 END) AS incomplete
                FROM ats_sources
                WHERE support_status = 'SUPPORTED'
                GROUP BY provider
                ORDER BY total DESC, provider
                """
            ).fetchall()

            recent_rows = connection.execute(
                """
                SELECT s.provider AS provider,
                       COUNT(*) AS runs,
                       SUM(CASE WHEN r.completion_status = 'SUCCESS' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN r.completion_status = 'FAILED' THEN 1 ELSE 0 END) AS failed
                FROM scan_runs r
                JOIN ats_sources s ON s.id = r.source_id
                WHERE r.started_at >= ?
                GROUP BY s.provider
                """,
                (since,),
            ).fetchall()

            counts = connection.execute(
                """
                SELECT (SELECT COUNT(*) FROM jobs) AS jobs,
                       (SELECT COUNT(*) FROM job_observations) AS observations,
                       (SELECT COUNT(*) FROM job_versions) AS versions,
                       (SELECT COUNT(*) FROM job_events) AS events,
                       (SELECT COUNT(*) FROM jobs WHERE potential_repost_of_job_id IS NOT NULL) AS reposts,
                       (SELECT COUNT(*) FROM jobs WHERE java_keyword_match = 1) AS java_matches,
                       (SELECT COUNT(*) FROM jobs WHERE location_match = 'CONFIRMED') AS confirmed,
                       (SELECT COUNT(*) FROM jobs
                         WHERE java_keyword_match = 1 AND location_match = 'CONFIRMED') AS java_confirmed,
                       (SELECT COUNT(*) FROM scan_runs
                         WHERE completion_status = 'SUCCESS' AND record_count = 0) AS empty_success,
                       (SELECT COUNT(*) FROM (
                          SELECT source_id FROM scan_runs
                          WHERE completion_status = 'FAILED'
                          GROUP BY source_id HAVING COUNT(*) >= 2)) AS repeated_failures
                """
            ).fetchone()

            failure_rows = connection.execute(
                """
                SELECT s.provider AS provider, r.error_information AS information
                FROM scan_runs r
                JOIN ats_sources s ON s.id = r.source_id
                WHERE r.completion_status = 'FAILED'
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (_FAILURE_SAMPLE_LIMIT,),
            ).fetchall()
        finally:
            connection.rollback()
    finally:
        connection.close()

    if schema_version is None:
        raise RuntimeError(f"database is not initialized: {target}")

    window = float(window_minutes)
    recent_by_provider = {row["provider"]: row for row in recent_rows}
    recent_runs = sum(row["runs"] for row in recent_rows)
    recent_success = sum(row["success"] or 0 for row in recent_rows)
    recent_failed = sum(row["failed"] or 0 for row in recent_rows)
    recent_rate = recent_runs / window

    providers: list[ProviderProgress] = []
    supported_total = 0
    supported_attempted = 0
    for row in provider_rows:
        total = int(row["total"] or 0)
        attempted = int(row["attempted"] or 0)
        remaining = max(0, total - attempted)
        recent = recent_by_provider.get(row["provider"])
        provider_recent_runs = int(recent["runs"]) if recent is not None else 0
        provider_rate = provider_recent_runs / window
        supported_total += total
        supported_attempted += attempted
        if remaining == 0:
            provider_eta: float | None = 0.0
        elif provider_rate > 0:
            provider_eta = remaining / provider_rate
        else:
            provider_eta = None
        providers.append(
            ProviderProgress(
                provider=row["provider"],
                total=total,
                attempted=attempted,
                never_attempted=remaining,
                succeeded=int(row["succeeded"] or 0),
                failed=int(row["failed"] or 0),
                incomplete=int(row["incomplete"] or 0),
                recent_runs=provider_recent_runs,
                recent_success=int(recent["success"] or 0) if recent is not None else 0,
                recent_failed=int(recent["failed"] or 0) if recent is not None else 0,
                recent_boards_per_minute=provider_rate,
                eta_minutes=provider_eta,
            )
        )

    supported_remaining = max(0, supported_total - supported_attempted)
    first_started = datetime_from_storage(totals["first_started"])
    last_ended = datetime_from_storage(totals["last_ended"])
    if first_started is not None:
        elapsed_minutes = max(0.0, (current - first_started).total_seconds() / 60.0)
    else:
        elapsed_minutes = 0.0
    overall_rate = (supported_attempted / elapsed_minutes) if elapsed_minutes > 0 else 0.0
    seconds_since_last = (
        max(0.0, (current - last_ended).total_seconds()) if last_ended is not None else None
    )

    failure_counts: dict[str, int] = {}
    failure_providers: dict[str, dict[str, int]] = {}
    for row in failure_rows:
        label = _failure_label(row["information"])
        failure_counts[label] = failure_counts.get(label, 0) + 1
        per_provider = failure_providers.setdefault(label, {})
        provider = row["provider"]
        per_provider[provider] = per_provider.get(provider, 0) + 1
    ranked_failures = sorted(failure_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    failure_reasons = tuple(
        FailureReason(
            reason=label,
            count=count,
            by_provider=tuple(
                sorted(failure_providers[label].items(), key=lambda item: (-item[1], item[0]))
            ),
        )
        for label, count in ranked_failures
    )

    if supported_remaining == 0:
        eta: float | None = 0.0
    elif recent_rate > 0:
        eta = supported_remaining / recent_rate
    else:
        eta = None
    wal_path = target.parent / f"{target.name}-wal"

    return MonitorSnapshot(
        captured_at=current,
        database_path=str(target),
        database_bytes=target.stat().st_size,
        wal_bytes=wal_path.stat().st_size if wal_path.exists() else 0,
        schema_version=schema_version,
        supported_total=supported_total,
        supported_attempted=supported_attempted,
        supported_remaining=supported_remaining,
        scan_runs=int(totals["runs"] or 0),
        scan_runs_success=int(totals["success"] or 0),
        scan_runs_failed=int(totals["failed"] or 0),
        scan_runs_incomplete=int(totals["incomplete"] or 0),
        first_scan_started_at=first_started,
        last_scan_ended_at=last_ended,
        seconds_since_last_scan=seconds_since_last,
        stalled=bool(
            supported_remaining > 0
            and seconds_since_last is not None
            and seconds_since_last > stall_seconds
        ),
        window_minutes=window,
        recent_runs=recent_runs,
        recent_success=recent_success,
        recent_failed=recent_failed,
        overall_boards_per_minute=overall_rate,
        recent_boards_per_minute=recent_rate,
        eta_minutes=eta,
        providers=tuple(providers),
        failure_reasons=failure_reasons,
        empty_success_scans=int(counts["empty_success"] or 0),
        repeated_failure_sources=int(counts["repeated_failures"] or 0),
        jobs=int(counts["jobs"] or 0),
        observations=int(counts["observations"] or 0),
        job_versions=int(counts["versions"] or 0),
        job_events=int(counts["events"] or 0),
        repost_candidates=int(counts["reposts"] or 0),
        java_matches=int(counts["java_matches"] or 0),
        confirmed_locations=int(counts["confirmed"] or 0),
        java_confirmed=int(counts["java_confirmed"] or 0),
    )


def _format_duration(minutes: float | None) -> str:
    if minutes is None:
        return "unknown"
    if minutes <= 0:
        return "done"
    if minutes < 1:
        return f"{minutes * 60:.0f}s"
    if minutes < 90:
        return f"{minutes:.0f}m"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / (60 * 24):.1f}d"


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def _progress_bar(fraction: float, width: int = 28) -> str:
    clamped = min(1.0, max(0.0, fraction))
    filled = int(round(clamped * width))
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def render_text(snapshot: MonitorSnapshot) -> str:
    fraction = (
        snapshot.supported_attempted / snapshot.supported_total
        if snapshot.supported_total
        else 0.0
    )
    captured = snapshot.captured_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"ATS Finder monitor  {captured}",
        f"  db {snapshot.database_path} ({_format_bytes(snapshot.database_bytes)},"
        f" wal {_format_bytes(snapshot.wal_bytes)}, schema v{snapshot.schema_version})",
        f"  sources {snapshot.supported_total} supported |"
        f" scan runs {snapshot.scan_runs}"
        f" (ok {snapshot.scan_runs_success}, fail {snapshot.scan_runs_failed},"
        f" incomplete {snapshot.scan_runs_incomplete})",
        f"  progress {_progress_bar(fraction)} {fraction * 100:5.1f}%"
        f"  {snapshot.supported_attempted}/{snapshot.supported_total}"
        f"  remaining {snapshot.supported_remaining}",
        f"  rate: recent {snapshot.recent_boards_per_minute:.1f}/min"
        f" (last {snapshot.window_minutes:g}m),"
        f" overall {snapshot.overall_boards_per_minute:.1f}/min"
        f" | ETA {_format_duration(snapshot.eta_minutes)}",
        "",
        f"  {'provider':<15}{'total':>7}{'tried':>7}{'ok':>7}{'fail':>7}{'inc':>6}"
        f"{'never':>8}{'/min':>7}  eta",
    ]
    for provider in snapshot.providers:
        lines.append(
            f"  {provider.provider:<15}{provider.total:>7}{provider.attempted:>7}"
            f"{provider.succeeded:>7}{provider.failed:>7}{provider.incomplete:>6}"
            f"{provider.never_attempted:>8}{provider.recent_boards_per_minute:>7.1f}"
            f"  {_format_duration(provider.eta_minutes)}"
        )
    lines.append("")
    if snapshot.failure_reasons:
        reasons = "; ".join(
            f"{item.reason} x{item.count} ("
            + ", ".join(f"{name} {count}" for name, count in item.by_provider)
            + ")"
            for item in snapshot.failure_reasons
        )
        lines.append(f"  failures: {reasons}")
    else:
        lines.append("  failures: none")
    lines.append(
        f"  data: jobs {snapshot.jobs}, observations {snapshot.observations},"
        f" versions {snapshot.job_versions}, events {snapshot.job_events}"
    )
    lines.append(
        f"  evidence: java {snapshot.java_matches},"
        f" confirmed location {snapshot.confirmed_locations},"
        f" java+confirmed {snapshot.java_confirmed}, repost links {snapshot.repost_candidates}"
    )
    lines.append(
        f"  integrity: empty-success scans {snapshot.empty_success_scans},"
        f" repeated-failure sources {snapshot.repeated_failure_sources}"
    )
    if snapshot.seconds_since_last_scan is not None:
        lines.append(
            f"  last completed scan {_format_duration(snapshot.seconds_since_last_scan / 60)} ago"
        )
    if snapshot.stalled:
        lines.append(
            "  WARNING: no scan has completed recently while sources remain;"
            " check the worker process and logs."
        )
    return "\n".join(lines)



def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def sample_record(
    snapshot: MonitorSnapshot,
    *,
    event: str,
    run_id: str,
    pid: int,
) -> dict[str, Any]:
    """Flatten a snapshot into one self-describing JSONL row."""

    record: dict[str, Any] = {"event": event, "run_id": run_id, "pid": pid}
    record.update(asdict(snapshot))
    return record


class MonitorRecorder:
    """Background telemetry writer that appends one JSONL sample per interval.

    The thread writes an immediate ``start`` sample, periodic ``sample`` rows,
    and a final ``stop`` row. Sampling and writing failures are logged and
    swallowed so telemetry can never interrupt or crash a scan.
    """

    def __init__(
        self,
        database_path: str | Path,
        log_path: str | Path,
        *,
        interval_seconds: float = 60.0,
        window_minutes: float = 15.0,
        stall_seconds: float = 300.0,
        max_bytes: int = 50 * 1024 * 1024,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if window_minutes <= 0:
            raise ValueError("window_minutes must be positive")
        if stall_seconds <= 0:
            raise ValueError("stall_seconds must be positive")
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        self._database_path = str(database_path)
        self._log_path = Path(log_path).expanduser()
        self._interval = float(interval_seconds)
        self._window_minutes = float(window_minutes)
        self._stall_seconds = float(stall_seconds)
        self._max_bytes = int(max_bytes)
        self._logger = logger or LOGGER
        self._run_id = new_run_id()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._samples = 0
        self._failures = 0
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def failures(self) -> int:
        return self._failures

    def start(self) -> "MonitorRecorder":
        if self._thread is not None:
            return self
        self.sample_now("start")
        self._thread = threading.Thread(
            target=self._loop, name="ats-finder-telemetry", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                self._logger.warning(
                    "telemetry thread did not stop within %.1fs", timeout
                )
                return
        self.sample_now("stop")

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.sample_now("sample")

    def sample_now(self, event: str = "sample") -> bool:
        try:
            snapshot = collect_snapshot(
                self._database_path,
                window_minutes=self._window_minutes,
                stall_seconds=self._stall_seconds,
            )
        except Exception as exc:  # telemetry must never break the scan
            self._note_failure("sample", exc)
            return False
        record = sample_record(
            snapshot, event=event, run_id=self._run_id, pid=os.getpid()
        )
        with self._write_lock:
            try:
                self._rotate_if_needed()
                with self._log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(record, default=_json_default, ensure_ascii=False)
                        + "\n"
                    )
            except OSError as exc:
                self._note_failure("write", exc)
                return False
            self._samples += 1
        if event != "sample":
            eta = (
                "unknown"
                if snapshot.eta_minutes is None
                else f"{snapshot.eta_minutes:.0f}m"
            )
            self._logger.info(
                "telemetry %s sample %s/%s attempted, remaining %s, eta %s -> %s",
                event,
                snapshot.supported_attempted,
                snapshot.supported_total,
                snapshot.supported_remaining,
                eta,
                self._log_path,
            )
        return True

    def _note_failure(self, stage: str, exc: BaseException) -> None:
        self._failures += 1
        if self._failures == 1 or self._failures % 60 == 0:
            self._logger.warning(
                "telemetry %s failed (%s of %s): %s: %s",
                stage,
                self._failures,
                self._samples,
                type(exc).__name__,
                exc,
            )

    def _rotate_if_needed(self) -> None:
        if self._max_bytes == 0:
            return
        if (
            not self._log_path.exists()
            or self._log_path.stat().st_size < self._max_bytes
        ):
            return
        backup = self._log_path.with_name(f"{self._log_path.name}.1")
        backup.unlink(missing_ok=True)
        self._log_path.replace(backup)
        self._logger.info("rotated telemetry log to %s", backup)


def start_monitor_recorder(
    settings: Settings,
    *,
    logger: logging.Logger | None = None,
) -> MonitorRecorder | None:
    """Start automatic telemetry for a scan run, or return None when disabled."""

    log_path = settings.monitor_log_path
    if log_path is None:
        return None
    try:
        recorder = MonitorRecorder(
            settings.database_path,
            log_path,
            interval_seconds=float(settings.monitor_interval_seconds),
            logger=logger,
        )
    except OSError as exc:
        (logger or LOGGER).warning(
            "telemetry disabled; cannot use %s: %s", log_path, exc
        )
        return None
    return recorder.start()


@contextmanager
def monitoring_session(
    settings: Settings,
    *,
    logger: logging.Logger | None = None,
) -> Iterator[MonitorRecorder | None]:
    """Record telemetry for the duration of a block, then write a stop sample."""

    recorder = start_monitor_recorder(settings, logger=logger)
    try:
        yield recorder
    finally:
        if recorder is not None:
            recorder.stop()


def read_samples(path: str | Path) -> list[dict[str, Any]]:
    """Read a telemetry JSONL file, skipping blank or corrupt lines."""

    source = Path(path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"telemetry log not found: {source}")
    rows: list[dict[str, Any]] = []
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def render_history(path: str | Path, *, recent: int = 10) -> str:
    """Summarize a saved telemetry file per run, plus the latest samples."""

    source = Path(path).expanduser()
    rows = read_samples(source)
    if not rows:
        return f"no telemetry samples in {source}"

    sessions: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sessions.setdefault(str(row.get("run_id", "unknown")), []).append(row)

    lines = [
        f"Telemetry history {source} ({len(rows)} samples,"
        f" {len(sessions)} run(s))"
    ]
    for run_id, samples in sessions.items():
        first, last = samples[0], samples[-1]
        attempted = int(last.get("supported_attempted") or 0) - int(
            first.get("supported_attempted") or 0
        )
        stalled = sum(1 for sample in samples if sample.get("stalled"))
        finished = last.get("event") == "stop"
        lines.append(
            f"  run {run_id}: {first.get('captured_at')} -> {last.get('captured_at')}"
            f" | samples {len(samples)}, boards scanned {attempted:+d},"
            f" final rate {float(last.get('recent_boards_per_minute') or 0):.1f}/min,"
            f" stalled samples {stalled}"
            + ("" if finished else ", no stop sample (still running or killed)")
        )

    def _sample_line(row: dict[str, Any]) -> str:
        return (
            f"  {row.get('captured_at')} {str(row.get('event')):<6}"
            f" {row.get('supported_attempted')}/{row.get('supported_total')}"
            f" rem {row.get('supported_remaining')}"
            f" {float(row.get('recent_boards_per_minute') or 0):5.1f}/min"
            f" eta {_format_duration(row.get('eta_minutes'))}"
        )

    lines.append("Latest samples:")
    lines.extend(_sample_line(row) for row in rows[-max(1, recent) :])
    return "\n".join(lines)

