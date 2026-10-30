from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ats_finder.config import Settings
from ats_finder.scanner import BoardScanner
from ats_finder.worker import run_worker


def test_run_worker_cancels_in_flight_scans_on_stop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        database_path=tmp_path / "worker.sqlite3",
        monitor_log_path=None,
    )

    async def hang(self, *, limit=None):
        del self, limit
        await asyncio.sleep(3600)

    monkeypatch.setattr(BoardScanner, "scan_due", hang)

    async def scenario() -> None:
        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_worker(settings, batch_size=1, stop_event=stop)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(worker, timeout=5)

    asyncio.run(scenario())


def test_run_worker_until_complete_exits_when_no_due_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        database_path=tmp_path / "worker.sqlite3",
        monitor_log_path=None,
    )
    calls = 0

    async def no_due(self, *, limit=None):
        nonlocal calls
        del self, limit
        calls += 1
        return []

    monkeypatch.setattr(BoardScanner, "scan_due", no_due)

    count = asyncio.run(
        run_worker(settings, batch_size=1, until_complete=True)
    )

    assert count == 0
    assert calls == 1


def test_run_worker_rejects_once_with_until_complete(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "worker.sqlite3",
        monitor_log_path=None,
    )

    with pytest.raises(ValueError, match="mutually exclusive"):
        asyncio.run(
            run_worker(settings, once=True, until_complete=True)
        )


def test_until_complete_does_not_report_success_after_an_unexpected_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        database_path=tmp_path / "worker.sqlite3",
        monitor_log_path=None,
    )

    async def fail(self, *, limit=None):
        del self, limit
        raise ExceptionGroup("scanner failed", [RuntimeError("boom")])

    monkeypatch.setattr(BoardScanner, "scan_due", fail)

    with pytest.raises(ExceptionGroup, match="before all due sources completed"):
        asyncio.run(
            run_worker(settings, batch_size=1, until_complete=True)
        )
