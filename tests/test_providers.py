from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from ats_finder.config import Settings
from ats_finder.models import (
    PublicationSemantics,
    SourceInput,
    SourceSupportStatus,
    WorkplaceType,
)
from ats_finder.providers.ashby import parse_ashby_job
from ats_finder.providers.base import PaginationError, ProviderParseError
from ats_finder.providers.greenhouse import parse_greenhouse_job
from ats_finder.providers.lever import LeverAdapter, parse_lever_job
from ats_finder.providers.workable import WorkableAdapter, parse_workable_job

from conftest import FIXED_NOW, EngineHarness


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_greenhouse_fixture_parses_documented_original_publication() -> None:
    job = parse_greenhouse_job(
        _fixture("greenhouse_job.json"), fallback_company="Fallback Company"
    )

    assert job.provider_job_id == "1001"
    assert job.company == "Acme Canada"
    assert job.title == "Senior Java Engineer"
    assert job.description == "Build Java services & APIs."
    assert job.location == "Ottawa, Ontario; Ottawa, Ontario, Canada; Ottawa HQ"
    assert job.workplace_type is WorkplaceType.ONSITE
    assert job.published_at == datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
    assert job.publication_is_verified is True
    assert job.publication_semantics is PublicationSemantics.ORIGINAL_PUBLICATION
    assert "first_published" in (job.publication_source or "")


def test_lever_fixture_uses_structured_location_but_not_created_at_as_publication() -> None:
    job = parse_lever_job(_fixture("lever_job.json"), fallback_company="Acme")

    assert job.provider_job_id == "lever-job-1"
    assert job.company == "Acme"
    assert job.location == "Remote - Canada; Ottawa, Ontario"
    assert job.workplace_type is WorkplaceType.REMOTE
    assert job.canada_remote_eligibility is True
    assert "Build Java services" in job.description
    assert "Ship reliable APIs" in job.description
    assert job.published_at is None
    assert job.publication_source is None
    assert job.publication_semantics is None
    assert job.publication_is_verified is False


def test_ashby_fixture_parses_republication_and_canada_remote_evidence() -> None:
    job = parse_ashby_job(_fixture("ashby_job.json"), fallback_company="Acme")

    assert job.provider_job_id == "ashby-job-1"
    assert job.application_url.endswith("/ashby-job-1/application")
    assert job.location == "Remote; Ottawa, Ontario; CA; Canada"
    assert job.workplace_type is WorkplaceType.REMOTE
    assert job.canada_remote_eligibility is True
    assert job.description == "Build distributed Java systems."
    assert job.published_at == datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
    assert job.publication_is_verified is True
    assert job.publication_semantics is PublicationSemantics.REPUBLICATION
    assert "publishedAt" in (job.publication_source or "")


def test_ashby_rejects_identifier_that_disagrees_with_official_url() -> None:
    payload = _fixture("ashby_job.json")
    payload["id"] = "different-id"

    with pytest.raises(ProviderParseError, match="does not match"):
        parse_ashby_job(payload, fallback_company="Acme")


class _JSONResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return deepcopy(self._payload)


class _LeverPagingClient:
    def __init__(self, pages: dict[int, list[object]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _JSONResponse:
        assert params is not None
        self.calls.append((url, dict(params), headers))
        return _JSONResponse(self.pages[int(params["skip"])])


def _lever_source(harness: EngineHarness):
    return harness.database.upsert_source(
        SourceInput(
            provider="lever",
            company="Acme",
            board_identifier="global:acme",
            career_page_url="https://jobs.lever.co/acme",
            support_status=SourceSupportStatus.SUPPORTED,
        ),
        now=FIXED_NOW,
    )


def _lever_item(provider_job_id: str) -> dict[str, object]:
    payload = _fixture("lever_job.json")
    payload["id"] = provider_job_id
    payload["applyUrl"] = f"https://jobs.lever.co/acme/{provider_job_id}/apply"
    payload["hostedUrl"] = f"https://jobs.lever.co/acme/{provider_job_id}"
    return payload


def test_lever_adapter_collects_every_page_until_a_short_terminal_page(
    harness: EngineHarness,
) -> None:
    source = _lever_source(harness)
    client = _LeverPagingClient(
        {
            0: [_lever_item("lever-1"), _lever_item("lever-2")],
            2: [_lever_item("lever-3")],
        }
    )
    settings = Settings(lever_page_size=2, max_pages=5)

    result = asyncio.run(LeverAdapter().fetch(source, client, settings))

    assert result.complete is True
    assert result.request_count == 2
    assert result.provider_metadata == {"pages": 2}
    assert [job.provider_job_id for job in result.jobs] == [
        "lever-1",
        "lever-2",
        "lever-3",
    ]
    assert [call[1] for call in client.calls] == [
        {"mode": "json", "skip": 0, "limit": 2},
        {"mode": "json", "skip": 2, "limit": 2},
    ]


def test_lever_adapter_rejects_repeated_identifier_across_pages(
    harness: EngineHarness,
) -> None:
    source = _lever_source(harness)
    client = _LeverPagingClient(
        {
            0: [_lever_item("lever-1"), _lever_item("lever-2")],
            2: [_lever_item("lever-2")],
        }
    )
    settings = Settings(lever_page_size=2, max_pages=5)

    with pytest.raises(PaginationError, match="repeated job id lever-2"):
        asyncio.run(LeverAdapter().fetch(source, client, settings))


def test_lever_adapter_rejects_unproven_completion_at_page_limit(
    harness: EngineHarness,
) -> None:
    source = _lever_source(harness)
    client = _LeverPagingClient(
        {
            0: [_lever_item("lever-1"), _lever_item("lever-2")],
            2: [_lever_item("lever-3"), _lever_item("lever-4")],
        }
    )
    settings = Settings(lever_page_size=2, max_pages=2)

    with pytest.raises(PaginationError, match="exceeded the configured 2 page limit"):
        asyncio.run(LeverAdapter().fetch(source, client, settings))


def _provider_source(
    harness: EngineHarness,
    provider: str,
    board_identifier: str,
    url: str,
):
    return harness.database.upsert_source(
        SourceInput(
            provider=provider,
            company="Acme",
            board_identifier=board_identifier,
            career_page_url=url,
            support_status=SourceSupportStatus.SUPPORTED,
        ),
        now=FIXED_NOW,
    )


def _workable_item(shortcode: str) -> dict[str, object]:
    return {
        "shortcode": shortcode,
        "title": "Java Backend Engineer",
        "description": "<p>Build Java services.</p>",
        "url": f"https://apply.workable.com/acme/j/{shortcode}/apply/",
        "published_on": "2026-10-27",
        "workplace_type": "remote",
        "telecommuting": True,
        "city": "Remote",
        "state": "Ontario",
        "country": "Canada",
    }


def test_workable_validates_day_precision_but_does_not_invent_a_timestamp() -> None:
    job = parse_workable_job(_workable_item("ABC123"), fallback_company="Acme")

    assert job.provider_job_id == "ABC123"
    assert job.workplace_type is WorkplaceType.REMOTE
    assert job.canada_remote_eligibility is True
    assert job.published_at is None
    assert job.publication_is_verified is False


class _WorkableClient:
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        min_interval: float | None = None,
    ) -> _JSONResponse:
        assert url == "https://apply.workable.com/api/v1/widget/accounts/acme"
        assert params == {"details": "true"}
        assert min_interval == 1.0
        del headers
        return _JSONResponse(
            {"name": "Acme Canada", "jobs": [_workable_item("ABC123")]}
        )


def test_workable_adapter_certifies_single_response_snapshot(
    harness: EngineHarness,
) -> None:
    source = _provider_source(
        harness, "workable", "acme", "https://apply.workable.com/acme/"
    )
    result = asyncio.run(
        WorkableAdapter().fetch(source, _WorkableClient(), Settings())
    )

    assert result.request_count == 1
    assert len(result.jobs) == 1
    assert result.provider_metadata["day_only_publication_dates_ignored"] == 1

