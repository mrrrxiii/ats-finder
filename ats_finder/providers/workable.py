from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import quote

from ats_finder.config import Settings
from ats_finder.models import ATSSource, NormalizedJob, WorkplaceType
from ats_finder.providers.base import FetchResult, ProviderParseError, RespectfulAsyncClient
from ats_finder.providers.text import html_to_text


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_WORKPLACE_TYPES = {
    "on_site": WorkplaceType.ONSITE,
    "hybrid": WorkplaceType.HYBRID,
    "remote": WorkplaceType.REMOTE,
}


def _required_string(item: dict[str, object], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProviderParseError(
            f"Workable job has no non-empty string {field}"
        )
    return value.strip()


def _optional_string(item: dict[str, object], field: str) -> str:
    value = item.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProviderParseError(f"Workable job {field} is not a string")
    return value.strip()


def _validate_published_on(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise ProviderParseError(
            "Workable published_on must be a YYYY-MM-DD calendar date"
        )
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ProviderParseError(
            "Workable published_on is not a valid calendar date"
        ) from exc
    return True


def _workplace(item: dict[str, object]) -> WorkplaceType:
    value = item.get("workplace_type")
    telecommuting = item.get("telecommuting")
    if telecommuting is not None and not isinstance(telecommuting, bool):
        raise ProviderParseError("Workable job telecommuting is not a boolean")

    if value is None:
        return WorkplaceType.REMOTE if telecommuting is True else WorkplaceType.UNKNOWN
    if not isinstance(value, str):
        raise ProviderParseError("Workable job workplace_type is not a string")
    normalized = value.strip().casefold()
    try:
        workplace = _WORKPLACE_TYPES[normalized]
    except KeyError as exc:
        raise ProviderParseError(
            f"Workable job has unsupported workplace_type {value!r}"
        ) from exc
    if workplace is WorkplaceType.REMOTE and telecommuting is False:
        raise ProviderParseError(
            "Workable workplace_type remote contradicts telecommuting=false"
        )
    if workplace is WorkplaceType.ONSITE and telecommuting is True:
        raise ProviderParseError(
            "Workable workplace_type on_site contradicts telecommuting=true"
        )
    return workplace


def _location(item: dict[str, object]) -> tuple[str, str]:
    parts: list[str] = []
    country = ""
    for field in ("city", "state", "country"):
        value = _optional_string(item, field)
        if field == "country":
            country = value
        if value and value.casefold() not in {
            existing.casefold() for existing in parts
        }:
            parts.append(value)
    return "; ".join(parts), country


def parse_workable_job(item: object, *, fallback_company: str) -> NormalizedJob:
    if not isinstance(item, dict):
        raise ProviderParseError("Workable jobs array contains a non-object entry")

    shortcode = _required_string(item, "shortcode")
    title = _required_string(item, "title")
    application_url = _required_string(item, "url")
    description = item.get("description")
    if not isinstance(description, str):
        raise ProviderParseError(
            f"Workable job {shortcode} has no description despite details=true"
        )

    # This validates the provider contract but deliberately does not manufacture
    # a timestamp. A calendar date cannot prove membership in a rolling 24-hour
    # window in the absence of a documented time and timezone.
    _validate_published_on(item.get("published_on"))
    workplace = _workplace(item)
    location, country = _location(item)

    canada_remote_eligibility: bool | None = None
    if workplace in {WorkplaceType.REMOTE, WorkplaceType.HYBRID} and country:
        canada_remote_eligibility = country.casefold() in {"ca", "can", "canada"}

    return NormalizedJob(
        provider_job_id=shortcode,
        title=title,
        description=html_to_text(description),
        location=location,
        application_url=application_url,
        company=fallback_company,
        workplace_type=workplace,
        canada_remote_eligibility=canada_remote_eligibility,
        published_at=None,
        publication_source=None,
        publication_semantics=None,
        publication_is_verified=False,
    )


class WorkableAdapter:
    provider = "workable"

    @staticmethod
    def _subdomain(source: ATSSource) -> str:
        subdomain = source.board_identifier
        if subdomain.startswith("global:"):
            subdomain = subdomain.split(":", 1)[1]
        subdomain = subdomain.strip()
        if not subdomain:
            raise ProviderParseError("Workable source has an empty subdomain")
        return subdomain

    async def fetch(
        self,
        source: ATSSource,
        client: RespectfulAsyncClient,
        settings: Settings,
    ) -> FetchResult:
        del settings
        subdomain = self._subdomain(source)
        response = await client.get(
            f"https://www.workable.com/api/accounts/{quote(subdomain, safe='')}",
            params={"details": "true"},
            headers={"Accept": "application/json"},
        )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise ProviderParseError("Workable response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderParseError("Workable response is not an object")

        account_name = payload.get("name")
        if not isinstance(account_name, str):
            raise ProviderParseError("Workable response has no string account name")
        company = account_name.strip() or source.company
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list):
            raise ProviderParseError("Workable response has no jobs array")

        jobs: list[NormalizedJob] = []
        seen_shortcodes: set[str] = set()
        day_only_dates = 0
        for raw in raw_jobs:
            job = parse_workable_job(raw, fallback_company=company)
            if job.provider_job_id in seen_shortcodes:
                raise ProviderParseError(
                    f"Workable returned duplicate shortcode {job.provider_job_id}"
                )
            seen_shortcodes.add(job.provider_job_id)
            if isinstance(raw, dict) and raw.get("published_on") is not None:
                day_only_dates += 1
            jobs.append(job)

        return FetchResult(
            jobs=tuple(jobs),
            request_count=1,
            provider_metadata={
                "reported_total": len(raw_jobs),
                "day_only_publication_dates_ignored": day_only_dates,
            },
        )
