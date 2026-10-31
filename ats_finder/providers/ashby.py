from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from ats_finder.config import Settings
from ats_finder.filters import classify_location
from ats_finder.models import (
    ATSSource,
    LocationMatch,
    NormalizedJob,
    PublicationSemantics,
    WorkplaceType,
)
from ats_finder.providers.base import FetchResult, ProviderParseError, RespectfulAsyncClient
from ats_finder.providers.text import html_to_text


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderParseError("Ashby publishedAt is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ProviderParseError("Ashby publishedAt timestamp has no timezone")
    return parsed


def _workplace_type(value: object) -> WorkplaceType:
    normalized = str(value or "").strip().casefold().replace("-", "")
    if normalized == "remote":
        return WorkplaceType.REMOTE
    if normalized == "hybrid":
        return WorkplaceType.HYBRID
    if normalized == "onsite":
        return WorkplaceType.ONSITE
    return WorkplaceType.UNKNOWN


def _country_from_address(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    postal = value.get("postalAddress")
    if isinstance(postal, dict):
        country = postal.get("addressCountry")
        if isinstance(country, str) and country.strip():
            return country.strip()
    country = value.get("addressCountry")
    if isinstance(country, str) and country.strip():
        return country.strip()
    return None


def _is_canada(value: str) -> bool:
    return value.strip().casefold() in {"ca", "can", "canada"}


def _job_identifier(item: dict[str, object]) -> str:
    documented_url_identifier: str | None = None
    for field in ("jobUrl", "applyUrl"):
        value = item.get(field)
        if not isinstance(value, str):
            continue
        segments = [unquote(part) for part in urlparse(value).path.split("/") if part]
        if segments and segments[-1].casefold() in {"application", "apply"}:
            segments.pop()
        if len(segments) >= 2 and segments[-1]:
            documented_url_identifier = segments[-1]
            break
    response_id = item.get("id")
    if isinstance(response_id, str) and response_id.strip():
        response_id = response_id.strip()
        if documented_url_identifier and response_id != documented_url_identifier:
            raise ProviderParseError(
                "Ashby response id does not match the official job URL identifier"
            )
        return response_id
    if documented_url_identifier:
        return documented_url_identifier
    raise ProviderParseError("Ashby job has no stable identifier in jobUrl/applyUrl")


def parse_ashby_job(item: object, *, fallback_company: str) -> NormalizedJob:
    if not isinstance(item, dict):
        raise ProviderParseError("Ashby job entry is not an object")
    title = item.get("title")
    job_url = item.get("jobUrl")
    apply_url = item.get("applyUrl")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(job_url, str)
        or not job_url.strip()
    ):
        raise ProviderParseError("Ashby job is missing title or jobUrl")
    if not isinstance(apply_url, str):
        apply_url = job_url

    locations: list[str] = []
    primary = item.get("location")
    if isinstance(primary, str) and primary.strip():
        locations.append(primary.strip())
    countries: list[str] = []
    country = _country_from_address(item.get("address"))
    if country:
        countries.append(country)
    secondary = item.get("secondaryLocations")
    if isinstance(secondary, list):
        for value in secondary:
            if not isinstance(value, dict):
                continue
            label = value.get("location")
            if isinstance(label, str) and label.strip() and label.strip() not in locations:
                locations.append(label.strip())
            secondary_country = _country_from_address(value.get("address"))
            if secondary_country:
                countries.append(secondary_country)
    for country_name in countries:
        if all(country_name.casefold() not in location.casefold() for location in locations):
            locations.append(country_name)

    workplace = _workplace_type(item.get("workplaceType"))
    remote_eligibility: bool | None = None
    if workplace in {WorkplaceType.REMOTE, WorkplaceType.HYBRID}:
        if any(_is_canada(value) for value in countries):
            remote_eligibility = True
        elif countries:
            remote_eligibility = False

    description = item.get("descriptionPlain")
    if not isinstance(description, str):
        description = html_to_text(item.get("descriptionHtml"))
    published_at = _parse_datetime(item.get("publishedAt"))

    return NormalizedJob(
        provider_job_id=_job_identifier(item),
        title=title.strip(),
        description=description.strip(),
        location="; ".join(dict.fromkeys(locations)),
        application_url=apply_url,
        company=fallback_company,
        workplace_type=workplace,
        canada_remote_eligibility=remote_eligibility,
        published_at=published_at,
        publication_source=(
            "Ashby public Job Postings API: publishedAt" if published_at else None
        ),
        publication_semantics=(
            PublicationSemantics.REPUBLICATION
            if published_at
            else None
        ),
        publication_is_verified=published_at is not None,
    )


class AshbyAdapter:
    provider = "ashby"

    async def fetch(
        self,
        source: ATSSource,
        client: RespectfulAsyncClient,
        settings: Settings,
    ) -> FetchResult:
        board = source.board_identifier
        if board.startswith("global:"):
            board = board.split(":", 1)[1]
        if not board:
            raise ProviderParseError("Ashby source has an empty board name")
        response = await client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{board}",
            params={"includeCompensation": "false"},
            headers={"Accept": "application/json"},
        )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise ProviderParseError("Ashby response is not valid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ProviderParseError("Ashby response has no jobs array")
        if payload.get("apiVersion") != "1":
            raise ProviderParseError(
                f"Unsupported Ashby public API version {payload.get('apiVersion')!r}"
            )

        jobs: list[NormalizedJob] = []
        seen_ids: set[str] = set()
        unlisted_count = 0
        for raw in payload["jobs"]:
            if isinstance(raw, dict) and raw.get("isListed") is False:
                unlisted_count += 1
                continue
            job = parse_ashby_job(raw, fallback_company=source.company)
            if job.provider_job_id in seen_ids:
                raise ProviderParseError(f"Ashby returned duplicate job id {job.provider_job_id}")
            seen_ids.add(job.provider_job_id)
            jobs.append(job)

        if jobs and not any(
            not job.location.strip()
            or classify_location(
                job.location,
                "",
                job.workplace_type,
                job.canada_remote_eligibility,
            ).match
            is not LocationMatch.REJECTED
            for job in jobs
        ):
            return FetchResult(
                jobs=(),
                request_count=1,
                location_filtered=True,
                observed_record_count=len(jobs),
                provider_metadata={
                    "api_version": "1",
                    "unlisted_excluded": unlisted_count,
                },
            )

        return FetchResult(
            jobs=tuple(jobs),
            request_count=1,
            provider_metadata={
                "api_version": "1",
                "unlisted_excluded": unlisted_count,
            },
        )

