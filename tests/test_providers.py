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
from ats_finder.providers.ashby import AshbyAdapter, parse_ashby_job
from ats_finder.providers.base import PaginationError, ProviderParseError
from ats_finder.providers.greenhouse import (
    GreenhouseAdapter,
    parse_greenhouse_job,
)
from ats_finder.providers.lever import LeverAdapter, parse_lever_job
from ats_finder.providers.smartrecruiters import (
    SmartRecruitersAdapter,
    parse_smartrecruiters_job,
)
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


class _GreenhouseClient:
    def __init__(self, item: dict[str, object]) -> None:
        self.item = item
        self.calls: list[dict[str, str] | None] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _JSONResponse:
        assert url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        assert headers is None
        self.calls.append(params)
        return _JSONResponse(
            {"jobs": [self.item], "meta": {"total": 1}}
        )


class _AshbyClient:
    def __init__(self, job: dict[str, object]) -> None:
        self.payload = {"apiVersion": "1", "jobs": [job]}

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _JSONResponse:
        assert url == "https://api.ashbyhq.com/posting-api/job-board/acme"
        assert params == {"includeCompensation": "false"}
        del headers
        return _JSONResponse(self.payload)


def test_greenhouse_uses_summary_preflight_before_content(
    harness: EngineHarness,
) -> None:
    source = _provider_source(
        harness, "greenhouse", "acme", "https://boards.greenhouse.io/acme"
    )
    client = _GreenhouseClient(_fixture("greenhouse_job.json"))

    result = asyncio.run(
        GreenhouseAdapter().fetch(source, client, Settings())
    )

    assert result.request_count == 2
    assert len(result.jobs) == 1
    assert client.calls == [None, {"content": "true"}]


def test_greenhouse_skips_content_for_out_of_scope_locations(
    harness: EngineHarness,
) -> None:
    source = _provider_source(
        harness, "greenhouse", "acme", "https://boards.greenhouse.io/acme"
    )
    item = _fixture("greenhouse_job.json")
    item["location"] = {"name": "Austin, TX, United States"}
    item["offices"] = []
    client = _GreenhouseClient(item)

    result = asyncio.run(
        GreenhouseAdapter().fetch(source, client, Settings())
    )

    assert result.request_count == 1
    assert result.location_filtered is True
    assert result.observed_record_count == 1
    assert result.jobs == ()
    assert client.calls == [None]


def test_ashby_marks_out_of_scope_board_as_location_filtered(
    harness: EngineHarness,
) -> None:
    source = _provider_source(
        harness, "ashby", "acme", "https://jobs.ashbyhq.com/acme"
    )
    item = _fixture("ashby_job.json")
    item["location"] = "Austin, Texas"
    item["secondaryLocations"] = []
    item["address"] = {
        "postalAddress": {
            "addressLocality": "Austin",
            "addressRegion": "Texas",
            "addressCountry": "USA",
        }
    }
    item["workplaceType"] = "OnSite"

    result = asyncio.run(
        AshbyAdapter().fetch(source, _AshbyClient(item), Settings())
    )

    assert result.request_count == 1
    assert result.location_filtered is True
    assert result.observed_record_count == 1
    assert result.jobs == ()


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


def test_lever_marks_out_of_scope_board_as_location_filtered(
    harness: EngineHarness,
) -> None:
    source = _lever_source(harness)
    item = _lever_item("lever-1")
    item["categories"] = {
        "location": "Austin, Texas",
        "allLocations": ["Austin, Texas"],
    }
    item["country"] = "US"
    item["workplaceType"] = "on-site"
    client = _LeverPagingClient({0: [item]})

    result = asyncio.run(
        LeverAdapter().fetch(
            source,
            client,
            Settings(lever_page_size=2, max_pages=5),
        )
    )

    assert result.request_count == 1
    assert result.location_filtered is True
    assert result.observed_record_count == 1
    assert result.jobs == ()


def _smart_summary(
    posting_id: str,
    *,
    city: str,
    country: str,
    remote: bool,
    hybrid: bool = False,
) -> dict[str, object]:
    return {
        "id": posting_id,
        "name": "Java Platform Engineer",
        "releasedDate": "2026-10-27T14:30:00Z",
        "location": {
            "city": city,
            "country": country,
            "remote": remote,
            "hybrid": hybrid,
            "fullLocation": f"{city}, {country}",
        },
    }


def _smart_detail(posting_id: str) -> dict[str, object]:
    return {
        "id": posting_id,
        "name": "Java Platform Engineer",
        "postingUrl": f"https://jobs.smartrecruiters.com/Acme/{posting_id}",
        "applyUrl": (
            f"https://jobs.smartrecruiters.com/Acme/{posting_id}/apply"
        ),
        "active": True,
        "releasedDate": "2026-10-27T14:30:00Z",
        "company": {"name": "Acme Canada"},
        "location": {
            "city": "Remote",
            "region": "ON",
            "country": "ca",
            "remote": True,
            "hybrid": False,
            "fullLocation": "Remote, ON, Canada",
        },
        "jobAd": {
            "sections": {
                "jobDescription": {
                    "title": "The role",
                    "text": "<p>Build Java services.</p>",
                },
                "qualifications": {
                    "title": "Qualifications",
                    "text": "Java 17 experience",
                },
            }
        },
    }


class _SmartClient:
    def __init__(
        self,
        summaries: list[dict[str, object]],
        *,
        first_total: int | None = None,
        second_total: int | None = None,
    ) -> None:
        self.summaries = summaries
        self.first_total = first_total
        self.second_total = second_total
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _JSONResponse:
        del headers
        self.calls.append((url, params))
        if params is not None:
            offset = int(params["offset"])
            total = (
                self.second_total
                if self.second_total is not None and offset > 0
                else self.first_total
                if self.first_total is not None and offset == 0
                else len(self.summaries)
            )
            return _JSONResponse(
                {
                    "limit": 100,
                    "offset": offset,
                    "totalFound": total,
                    "content": self.summaries[offset : offset + 100],
                }
            )
        posting_id = url.rsplit("/", 1)[-1]
        return _JSONResponse(_smart_detail(posting_id))


def test_smartrecruiters_parses_detail_and_uses_job_description_url() -> None:
    job = parse_smartrecruiters_job(
        _smart_detail("smart-1"), fallback_company="Fallback"
    )

    assert job.provider_job_id == "smart-1"
    assert job.company == "Acme Canada"
    assert job.workplace_type is WorkplaceType.REMOTE
    assert job.canada_remote_eligibility is True
    assert job.publication_semantics is PublicationSemantics.ATS_PUBLISHED_AT
    assert job.publication_is_verified is True
    assert "Build Java services" in job.description
    assert job.application_url == (
        "https://jobs.smartrecruiters.com/Acme/smart-1"
    )


def test_smartrecruiters_fetches_details_only_for_location_candidates(
    harness: EngineHarness,
) -> None:
    source = _provider_source(
        harness,
        "smartrecruiters",
        "Acme",
        "https://jobs.smartrecruiters.com/Acme",
    )
    client = _SmartClient(
        [
            _smart_summary(
                "smart-1", city="Toronto", country="ca", remote=True
            ),
            _smart_summary(
                "smart-2", city="Austin", country="us", remote=False
            ),
        ]
    )

    result = asyncio.run(
        SmartRecruitersAdapter().fetch(
            source,
            client,
            Settings(max_pages=2),
        )
    )

    assert [job.provider_job_id for job in result.jobs] == ["smart-1"]
    assert result.request_count == 2
    assert result.provider_metadata["detail_requests"] == 1
    assert len(client.calls) == 2
    assert client.calls[1][1] is None
    assert client.calls[1][0].endswith("/smart-1")


def test_smartrecruiters_skips_all_details_when_locations_are_out_of_scope(
    harness: EngineHarness,
) -> None:
    source = _provider_source(
        harness,
        "smartrecruiters",
        "Acme",
        "https://jobs.smartrecruiters.com/Acme",
    )
    client = _SmartClient(
        [
            _smart_summary(
                "smart-1", city="Austin", country="us", remote=False
            ),
            _smart_summary(
                "smart-2", city="Berlin", country="de", remote=True
            ),
        ]
    )

    result = asyncio.run(
        SmartRecruitersAdapter().fetch(
            source,
            client,
            Settings(max_pages=2),
        )
    )

    assert result.request_count == 1
    assert result.location_filtered is True
    assert result.observed_record_count == 2
    assert result.jobs == ()
    assert result.provider_metadata["detail_requests"] == 0
    assert len(client.calls) == 1


def test_smartrecruiters_rejects_changed_total_during_pagination(
    harness: EngineHarness,
) -> None:
    source = _provider_source(
        harness,
        "smartrecruiters",
        "Acme",
        "https://jobs.smartrecruiters.com/Acme",
    )
    client = _SmartClient(
        [
            _smart_summary(
                "smart-1", city="Toronto", country="ca", remote=True
            )
        ],
        first_total=2,
        second_total=3,
    )

    with pytest.raises(PaginationError, match="pagination stopped"):
        asyncio.run(
            SmartRecruitersAdapter().fetch(
                source,
                client,
                Settings(max_pages=2),
            )
        )


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
    def __init__(self, jobs: list[object] | None = None) -> None:
        self.jobs = jobs if jobs is not None else [_workable_item("ABC123")]
        self.calls: list[dict[str, Any] | None] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        min_interval: float | None = None,
    ) -> _JSONResponse:
        assert url == "https://apply.workable.com/api/v1/widget/accounts/acme"
        assert min_interval == 1.0
        assert params in (None, {"details": "true"})
        self.calls.append(params)
        del headers
        return _JSONResponse(
            {"name": "Acme Canada", "jobs": self.jobs}
        )


def test_workable_adapter_certifies_single_response_snapshot(
    harness: EngineHarness,
) -> None:
    source = _provider_source(
        harness, "workable", "acme", "https://apply.workable.com/acme/"
    )
    client = _WorkableClient()
    result = asyncio.run(
        WorkableAdapter().fetch(source, client, Settings())
    )

    assert result.request_count == 2
    assert len(result.jobs) == 1
    assert result.provider_metadata["day_only_publication_dates_ignored"] == 1
    assert client.calls == [None, {"details": "true"}]


def test_workable_skips_descriptions_when_all_locations_are_out_of_scope(
    harness: EngineHarness,
) -> None:
    source = _provider_source(
        harness, "workable", "acme", "https://apply.workable.com/acme/"
    )
    non_target = {
        **_workable_item("US123"),
        "city": "Austin",
        "state": "Texas",
        "country": "United States",
        "workplace_type": "on_site",
        "telecommuting": False,
    }
    client = _WorkableClient([non_target])

    result = asyncio.run(
        WorkableAdapter().fetch(source, client, Settings())
    )

    assert result.request_count == 1
    assert result.location_filtered is True
    assert result.observed_record_count == 1
    assert result.jobs == ()
    assert client.calls == [None]

