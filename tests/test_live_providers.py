from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ats_finder.config import Settings
from ats_finder.db import Database
from ats_finder.http_client import RespectfulHTTPClient
from ats_finder.models import SourceInput, SourceSupportStatus
from ats_finder.providers import ADAPTERS


LIVE_BOARDS = (
    (
        "greenhouse",
        "Greenhouse",
        "greenhouse",
        "https://boards.greenhouse.io/greenhouse",
    ),
    (
        "lever",
        "Lever Demo",
        "global:leverdemo",
        "https://jobs.lever.co/leverdemo",
    ),
    (
        "ashby",
        "Ashby",
        "Ashby",
        "https://jobs.ashbyhq.com/Ashby",
    ),
    (
        "smartrecruiters",
        "JLI Consulting",
        "JLIConsulting",
        "https://jobs.smartrecruiters.com/JLIConsulting",
    ),
    (
        "workable",
        "ClickView",
        "clickview",
        "https://apply.workable.com/clickview/",
    ),
)


@pytest.mark.live
@pytest.mark.parametrize(
    ("provider", "company", "board_identifier", "career_url"),
    LIVE_BOARDS,
    ids=[item[0] for item in LIVE_BOARDS],
)
def test_official_public_provider_endpoint_returns_a_complete_snapshot(
    tmp_path: Path,
    provider: str,
    company: str,
    board_identifier: str,
    career_url: str,
) -> None:
    async def fetch() -> None:
        settings = Settings(
            request_timeout_seconds=15,
            max_retries=1,
            per_host_interval_seconds=0.05,
            max_pages=10,
        )
        database = Database(tmp_path / f"{provider}.sqlite3")
        try:
            source = database.upsert_source(
                SourceInput(
                    provider=provider,
                    company=company,
                    board_identifier=board_identifier,
                    career_page_url=career_url,
                    support_status=SourceSupportStatus.SUPPORTED,
                )
            )
            async with RespectfulHTTPClient(settings) as client:
                result = await ADAPTERS[provider].fetch(source, client, settings)
            assert result.complete is True
            assert result.request_count >= 1
            assert len({job.provider_job_id for job in result.jobs}) == len(result.jobs)
        finally:
            database.close()

    asyncio.run(asyncio.wait_for(fetch(), timeout=60))

