from __future__ import annotations

import asyncio
from collections.abc import Callable

from ats_finder.config import Settings
from ats_finder.models import NormalizedJob, SourceLastScanResult
from ats_finder.providers import ADAPTERS
from ats_finder.providers.base import FetchResult
from ats_finder.scanner import BoardScanner

from conftest import FIXED_NOW, EngineHarness


class _CompleteAdapter:
    provider = "greenhouse"

    def __init__(self, job: NormalizedJob) -> None:
        self.job = job

    async def fetch(self, source, client, settings) -> FetchResult:
        del source, client, settings
        return FetchResult(jobs=(self.job,), request_count=1)


def test_scan_attempt_returns_freshly_persisted_source_state(
    harness: EngineHarness,
    job_factory: Callable[..., NormalizedJob],
    monkeypatch,
) -> None:
    monkeypatch.setitem(ADAPTERS, "greenhouse", _CompleteAdapter(job_factory()))

    async def scan():
        scanner = BoardScanner(
            harness.database,
            Settings(per_host_interval_seconds=0),
            client=object(),
            now=lambda: FIXED_NOW,
        )
        return await scanner.scan_source(harness.source.id)

    attempt = asyncio.run(scan())

    assert attempt.outcome.status.value == "SUCCESS"
    assert attempt.source.last_scan_result is SourceLastScanResult.SUCCESS
    assert attempt.source.last_successful_scan_at == FIXED_NOW
    assert attempt.source.last_attempted_scan_at == FIXED_NOW
    assert attempt.source.consecutive_failures == 0
