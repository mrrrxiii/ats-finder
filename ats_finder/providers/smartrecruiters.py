from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from ats_finder.config import Settings
from ats_finder.models import (
    ATSSource,
    NormalizedJob,
    PublicationSemantics,
    WorkplaceType,
)
from ats_finder.providers.base import (
    FetchResult,
    PaginationError,
    ProviderParseError,
    RespectfulAsyncClient,
)
from ats_finder.providers.text import html_to_text


_PAGE_LIMIT = 100
_TEXT_SECTIONS = (
    "companyDescription",
    "jobDescription",
    "qualifications",
    "additionalInformation",
)


def _required_string(item: dict[str, object], field: str, context: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProviderParseError(
            f"SmartRecruiters {context} has no non-empty string {field}"
        )
    return value.strip()


def _parse_released_date(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProviderParseError(
            "SmartRecruiters releasedDate must be an ISO-8601 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderParseError(
            "SmartRecruiters releasedDate is not a valid ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderParseError("SmartRecruiters releasedDate has no timezone")
    return parsed


def _company_name(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    if not isinstance(value, dict):
        raise ProviderParseError("SmartRecruiters company is not an object")
    name = value.get("name")
    if name is None:
        return fallback
    if not isinstance(name, str):
        raise ProviderParseError("SmartRecruiters company.name is not a string")
    return name.strip() or fallback


def _location(value: object) -> tuple[str, WorkplaceType, bool | None]:
    if value is None:
        return "", WorkplaceType.UNKNOWN, None
    if not isinstance(value, dict):
        raise ProviderParseError("SmartRecruiters location is not an object")

    parts: list[str] = []
    for field in ("city", "region", "country"):
        part = value.get(field)
        if part is None:
            continue
        if not isinstance(part, str):
            raise ProviderParseError(
                f"SmartRecruiters location.{field} is not a string"
            )
        normalized = part.strip()
        if normalized and normalized.casefold() not in {
            existing.casefold() for existing in parts
        }:
            parts.append(normalized)

    remote = value.get("remote")
    if remote is None:
        return "; ".join(parts), WorkplaceType.UNKNOWN, None
    if not isinstance(remote, bool):
        raise ProviderParseError("SmartRecruiters location.remote is not a boolean")
    if not remote:
        return "; ".join(parts), WorkplaceType.ONSITE, None

    country = value.get("country")
    eligibility: bool | None = None
    if isinstance(country, str) and country.strip():
        eligibility = country.strip().casefold() in {"ca", "can", "canada"}
    return "; ".join(parts), WorkplaceType.REMOTE, eligibility


def _description(value: object) -> str:
    if not isinstance(value, dict):
        raise ProviderParseError("SmartRecruiters jobAd is not an object")
    sections = value.get("sections")
    if not isinstance(sections, dict):
        raise ProviderParseError("SmartRecruiters jobAd has no sections object")

    parts: list[str] = []
    for section_name in _TEXT_SECTIONS:
        section = sections.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ProviderParseError(
                f"SmartRecruiters jobAd section {section_name} is not an object"
            )
        title = section.get("title")
        text = section.get("text")
        if not isinstance(title, str) or not isinstance(text, str):
            raise ProviderParseError(
                f"SmartRecruiters jobAd section {section_name} "
                "does not contain string title and text fields"
            )
        normalized_title = html_to_text(title)
        normalized_text = html_to_text(text)
        if normalized_title:
            parts.append(normalized_title)
        if normalized_text:
            parts.append(normalized_text)

    videos = sections.get("videos")
    if videos is not None:
        if not isinstance(videos, dict):
            raise ProviderParseError(
                "SmartRecruiters jobAd videos section is not an object"
            )
        title = videos.get("title")
        urls = videos.get("urls")
        if not isinstance(title, str) or not isinstance(urls, list) or any(
            not isinstance(url, str) for url in urls
        ):
            raise ProviderParseError(
                "SmartRecruiters jobAd videos section has invalid title or urls"
            )

    documented = set(_TEXT_SECTIONS) | {"videos"}
    unknown_sections = sorted(set(sections) - documented)
    if unknown_sections:
        raise ProviderParseError(
            "SmartRecruiters jobAd contains undocumented sections: "
            + ", ".join(unknown_sections)
        )
    return "\n\n".join(parts)


def parse_smartrecruiters_job(
    item: object,
    *,
    fallback_company: str,
    list_released_date: object = None,
) -> NormalizedJob:
    if not isinstance(item, dict):
        raise ProviderParseError("SmartRecruiters posting detail is not an object")

    provider_id = _required_string(item, "id", "posting detail")
    title = _required_string(item, "name", "posting detail")
    posting_url = _required_string(item, "postingUrl", "posting detail")
    apply_url = _required_string(item, "applyUrl", "posting detail")
    active = item.get("active")
    if not isinstance(active, bool):
        raise ProviderParseError(
            "SmartRecruiters posting detail has no boolean active field"
        )
    if not active:
        raise PaginationError(
            f"SmartRecruiters posting {provider_id} became inactive during the scan"
        )

    # Both URLs are required by the PostingDetails contract. The application
    # URL is stored; validating postingUrl prevents a partial detail response
    # from being accepted as a complete job record.
    del posting_url
    description = _description(item.get("jobAd"))
    location, workplace_type, canada_remote_eligibility = _location(
        item.get("location")
    )

    detail_released_at = _parse_released_date(item.get("releasedDate"))
    listed_released_at = _parse_released_date(list_released_date)
    if (
        detail_released_at is not None
        and listed_released_at is not None
        and detail_released_at != listed_released_at
    ):
        raise PaginationError(
            f"SmartRecruiters releasedDate changed while fetching posting {provider_id}"
        )
    released_at = detail_released_at or listed_released_at

    return NormalizedJob(
        provider_job_id=provider_id,
        title=title,
        description=description,
        location=location,
        application_url=apply_url,
        company=_company_name(item.get("company"), fallback_company),
        workplace_type=workplace_type,
        canada_remote_eligibility=canada_remote_eligibility,
        published_at=released_at,
        publication_source=(
            "SmartRecruiters public Posting API: releasedDate"
            if released_at is not None
            else None
        ),
        publication_semantics=(
            PublicationSemantics.ATS_PUBLISHED_AT
            if released_at is not None
            else None
        ),
        publication_is_verified=released_at is not None,
    )


def _page_fields(
    payload: object,
    *,
    expected_offset: int,
) -> tuple[int, int, list[object]]:
    if not isinstance(payload, dict):
        raise ProviderParseError("SmartRecruiters postings page is not an object")

    values: dict[str, int] = {}
    for field in ("limit", "offset", "totalFound"):
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProviderParseError(
                f"SmartRecruiters postings page has no integer {field}"
            )
        values[field] = value

    limit = values["limit"]
    offset = values["offset"]
    total = values["totalFound"]
    if not 0 <= limit <= _PAGE_LIMIT:
        raise PaginationError(
            f"SmartRecruiters returned invalid page limit {limit}"
        )
    if offset != expected_offset:
        raise PaginationError(
            f"SmartRecruiters returned offset {offset}, expected {expected_offset}"
        )
    if total < 0 or offset < 0 or offset > total:
        raise PaginationError(
            f"SmartRecruiters returned invalid offset/total {offset}/{total}"
        )

    content = payload.get("content")
    if not isinstance(content, list):
        raise ProviderParseError(
            "SmartRecruiters postings page has no content array"
        )
    if len(content) > limit:
        raise PaginationError(
            "SmartRecruiters returned more postings than its reported page limit"
        )
    if total > offset and not content:
        raise PaginationError(
            "SmartRecruiters pagination stopped before totalFound was reached"
        )
    return limit, total, content


class SmartRecruitersAdapter:
    provider = "smartrecruiters"

    @staticmethod
    def _company_identifier(source: ATSSource) -> str:
        identifier = source.board_identifier
        if identifier.startswith("global:"):
            identifier = identifier.split(":", 1)[1]
        identifier = identifier.strip()
        if not identifier:
            raise ProviderParseError(
                "SmartRecruiters source has an empty company identifier"
            )
        return identifier

    async def fetch(
        self,
        source: ATSSource,
        client: RespectfulAsyncClient,
        settings: Settings,
    ) -> FetchResult:
        identifier = self._company_identifier(source)
        base = (
            "https://api.smartrecruiters.com/v1/companies/"
            f"{quote(identifier, safe='')}/postings"
        )
        summaries: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        expected_total: int | None = None
        offset = 0
        page_requests = 0

        for _ in range(settings.max_pages):
            response = await client.get(
                base,
                params={
                    "destination": "PUBLIC",
                    "limit": _PAGE_LIMIT,
                    "offset": offset,
                },
                headers={"Accept": "application/json"},
            )
            page_requests += 1
            try:
                payload: Any = response.json()
            except ValueError as exc:
                raise ProviderParseError(
                    "SmartRecruiters postings page is not valid JSON"
                ) from exc

            _, reported_total, content = _page_fields(
                payload,
                expected_offset=offset,
            )
            if expected_total is None:
                expected_total = reported_total
            elif reported_total != expected_total:
                raise PaginationError(
                    "SmartRecruiters totalFound changed across pagination: "
                    f"{expected_total} to {reported_total}"
                )

            for item in content:
                if not isinstance(item, dict):
                    raise ProviderParseError(
                        "SmartRecruiters content contains a non-object posting"
                    )
                posting_id = _required_string(item, "id", "posting summary")
                if posting_id in seen_ids:
                    raise PaginationError(
                        f"SmartRecruiters repeated posting id {posting_id}"
                    )
                # Validate an exposed date before spending requests on details.
                _parse_released_date(item.get("releasedDate"))
                seen_ids.add(posting_id)
                summaries.append(item)

            if len(seen_ids) > expected_total:
                raise PaginationError(
                    "SmartRecruiters returned more unique postings than totalFound"
                )
            if len(seen_ids) == expected_total:
                break
            offset += len(content)
        else:
            raise PaginationError(
                "SmartRecruiters pagination exceeded the configured "
                f"{settings.max_pages} page limit"
            )

        assert expected_total is not None
        if len(seen_ids) != expected_total:
            raise PaginationError(
                f"SmartRecruiters returned {len(seen_ids)} unique postings "
                f"but totalFound is {expected_total}"
            )

        jobs: list[NormalizedJob] = []
        for summary in summaries:
            posting_id = _required_string(summary, "id", "posting summary")
            response = await client.get(
                f"{base}/{quote(posting_id, safe='')}",
                headers={"Accept": "application/json"},
            )
            try:
                detail: Any = response.json()
            except ValueError as exc:
                raise ProviderParseError(
                    f"SmartRecruiters detail for posting {posting_id} "
                    "is not valid JSON"
                ) from exc
            job = parse_smartrecruiters_job(
                detail,
                fallback_company=source.company,
                list_released_date=summary.get("releasedDate"),
            )
            if job.provider_job_id != posting_id:
                raise PaginationError(
                    f"SmartRecruiters detail id {job.provider_job_id} "
                    f"does not match requested posting {posting_id}"
                )
            jobs.append(job)

        detail_requests = len(summaries)
        return FetchResult(
            jobs=tuple(jobs),
            request_count=page_requests + detail_requests,
            provider_metadata={
                "reported_total": expected_total,
                "pages": page_requests,
                "detail_requests": detail_requests,
            },
        )
