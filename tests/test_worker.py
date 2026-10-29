from __future__ import annotations

import asyncio
from pathlib import Path

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
