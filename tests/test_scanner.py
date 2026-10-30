from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta

import httpx
import pytest

from ats_finder.config import Settings
from ats_finder.models import NormalizedJob, SourceLastScanResult
from ats_finder.providers import ADAPTERS
from ats_finder.providers.base import FetchResult
from ats_finder.scanner import BoardScanner, SourceExcludedError

from conftest import FIXED_NOW, EngineHarness


class _CompleteAdapter:
    provider = "greenhouse"

    def __init__(self, job: NormalizedJob) -> None:
        self.job = job

    async def fetch(self, source, client, settings) -> FetchResult:
        del source, client, settings
        return FetchResult(jobs=(self.job,), request_count=1)


class _ExplodingAdapter:
    provider = "greenhouse"

    async def fetch(self, source, client, settings) -> FetchResult:
        del source, client, settings
        raise AssertionError("an excluded source must never be fetched")


class _MissingBoardAdapter:
    provider = "greenhouse"

    async def fetch(self, source, client, settings) -> FetchResult:
        del source, client, settings
        request = httpx.Request(
            "GET", "https://boards-api.greenhouse.io/v1/boards/missing/jobs"
        )
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError(
            "board not found", request=request, response=response
        )


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


def test_missing_board_gets_a_low_cost_recovery_check(
    harness: EngineHarness,
    monkeypatch,
) -> None:
    monkeypatch.setitem(ADAPTERS, "greenhouse", _MissingBoardAdapter())

    async def scan():
        scanner = BoardScanner(
            harness.database,
            Settings(per_host_interval_seconds=0),
            client=object(),
            now=lambda: FIXED_NOW,
        )
        return await scanner.scan_source(harness.source.id)

    attempt = asyncio.run(scan())

    assert attempt.outcome.status.value == "FAILED"
    assert attempt.source.consecutive_failures == 1
    assert attempt.source.next_scan_at == FIXED_NOW + timedelta(days=1)


def test_scan_source_rejects_an_excluded_board(
    harness: EngineHarness,
    monkeypatch,
) -> None:
    monkeypatch.setitem(ADAPTERS, "greenhouse", _ExplodingAdapter())
    harness.database.add_source_exclusion(
        harness.source.provider,
        harness.source.board_identifier,
        "not suitable",
        now=FIXED_NOW,
    )

    async def scan():
        scanner = BoardScanner(
            harness.database,
            Settings(per_host_interval_seconds=0),
            client=object(),
            now=lambda: FIXED_NOW,
        )
        return await scanner.scan_source(harness.source.id)

    with pytest.raises(SourceExcludedError, match="excluded by operator policy"):
        asyncio.run(scan())


def test_fair_due_sources_skips_excluded_boards(harness: EngineHarness) -> None:
    harness.database.add_source_exclusion(
        harness.source.provider,
        harness.source.board_identifier,
        "not suitable",
        now=FIXED_NOW,
    )
    scanner = BoardScanner(
        harness.database,
        Settings(per_host_interval_seconds=0),
        client=object(),
        now=lambda: FIXED_NOW,
    )

    assert scanner.fair_due_sources() == []


def test_scan_due_skips_a_source_excluded_after_selection(
    harness: EngineHarness,
    monkeypatch,
) -> None:
    harness.database.add_source_exclusion(
        harness.source.provider,
        harness.source.board_identifier,
        "not suitable",
        now=FIXED_NOW,
    )
    scanner = BoardScanner(
        harness.database,
        Settings(per_host_interval_seconds=0),
        client=object(),
        now=lambda: FIXED_NOW,
    )
    monkeypatch.setattr(
        scanner, "fair_due_sources", lambda **_: [harness.source]
    )

    assert asyncio.run(scanner.scan_due()) == []


def test_scan_source_refuses_a_removed_provider(
    harness: EngineHarness,
) -> None:
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE ats_sources SET provider = 'smartrecruiters' WHERE id = ?",
            (harness.source.id,),
        )

    async def scan():
        scanner = BoardScanner(
            harness.database,
            Settings(per_host_interval_seconds=0),
            client=object(),
            now=lambda: FIXED_NOW,
        )
        return await scanner.scan_source(harness.source.id)

    with pytest.raises(SourceExcludedError, match="removed from this project"):
        asyncio.run(scan())
