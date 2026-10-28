from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit


SUPPORTED_PROVIDERS = frozenset(
    {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"}
)

_PLATFORM_NAMES = {
    "greenhouse": "greenhouse",
    "lever": "lever",
    "ashby": "ashby",
    "workday": "workday",
    "smartrecruiters": "smartrecruiters",
    "workable": "workable",
    "bamboohr": "bamboohr",
    "rippling": "rippling",
    "jazzhr": "jazzhr",
    "breezy hr": "breezy_hr",
    "recruitee": "recruitee",
    "teamtailor": "teamtailor",
    "icims": "icims",
    "oracle": "oracle",
    "oracle cloud hcm": "oracle",
    "sap successfactors": "sap_successfactors",
    "dayforce": "dayforce",
    "adp": "adp",
    "jobvite": "jobvite",
}


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    provider: str
    company: str
    board_identifier: str
    career_page_url: str
    canonical_url: str
    support_status: str
    support_reason: str | None
    dataset_name: str | None = None
    dataset_revision: str | None = None


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Not a public HTTP(S) URL: {value!r}")
    scheme = "https"
    hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    port = parsed.port
    netloc = hostname if port is None or port == 443 else f"{hostname}:{port}"
    path = quote(unquote(parsed.path or "/"), safe="/%:@-._~")
    if path != "/":
        path = path.rstrip("/")
    query_pairs = []
    for key, values in sorted(parse_qs(parsed.query, keep_blank_values=True).items()):
        query_pairs.extend((key, value) for value in sorted(values))
    return urlunsplit((scheme, netloc, path, urlencode(query_pairs), ""))


def _humanize(value: str) -> str:
    words = value.replace("-", " ").replace("_", " ").strip()
    return " ".join(part.capitalize() for part in words.split()) or value


def _provider_from_host(host: str, platform: str) -> str:
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        return "greenhouse"
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        return "lever"
    if host == "jobs.ashbyhq.com":
        return "ashby"
    if host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"}:
        return "smartrecruiters"
    if host == "apply.workable.com":
        return "workable"
    return _PLATFORM_NAMES.get(platform.strip().casefold(), platform.strip().casefold().replace(" ", "_"))


def _extract_supported_identity(provider: str, canonical_url: str) -> tuple[str, str]:
    parsed = urlsplit(canonical_url)
    host = parsed.hostname or ""
    segments = [unquote(part) for part in parsed.path.split("/") if part]

    if provider == "greenhouse":
        token: str | None = None
        query = parse_qs(parsed.query)
        if segments[:2] == ["embed", "job_board"]:
            values = query.get("for", [])
            token = values[0] if values else None
        elif segments:
            token = segments[0]
        if not token or token.casefold() in {"jobs", "embed"}:
            raise ValueError("Greenhouse URL has no board token")
        return token, _humanize(token)

    if provider == "lever":
        if not segments:
            raise ValueError("Lever URL has no site name")
        site = segments[0]
        region = "eu" if host == "jobs.eu.lever.co" else "global"
        return f"{region}:{site}", _humanize(site)

    if provider == "ashby":
        if not segments:
            raise ValueError("Ashby URL has no job-board name")
        board = segments[0]
        return board, _humanize(board)

    if provider == "smartrecruiters":
        if not segments:
            raise ValueError("SmartRecruiters URL has no company identifier")
        identifier = segments[0]
        return identifier, _humanize(identifier)

    if provider == "workable":
        if not segments:
            raise ValueError("Workable URL has no account subdomain")
        subdomain = segments[0]
        return subdomain, _humanize(subdomain)

    raise ValueError(f"No supported identity extractor for {provider}")


def source_candidate(
    canonical_url: str,
    ats_platform: str,
    *,
    dataset_name: str | None = None,
    dataset_revision: str | None = None,
) -> SourceCandidate:
    normalized_url = canonicalize_url(canonical_url)
    parsed = urlsplit(normalized_url)
    provider = _provider_from_host(parsed.hostname or "", ats_platform)
    if provider in SUPPORTED_PROVIDERS:
        try:
            board_identifier, company = _extract_supported_identity(provider, normalized_url)
        except ValueError as exc:
            digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:24]
            return SourceCandidate(
                provider=provider,
                company=parsed.hostname or provider,
                board_identifier=f"invalid:{digest}",
                career_page_url=canonical_url.strip(),
                canonical_url=normalized_url,
                support_status="unsupported",
                support_reason=str(exc),
                dataset_name=dataset_name,
                dataset_revision=dataset_revision,
            )
        return SourceCandidate(
            provider=provider,
            company=company,
            board_identifier=board_identifier,
            career_page_url=canonical_url.strip(),
            canonical_url=normalized_url,
            support_status="supported",
            support_reason=None,
            dataset_name=dataset_name,
            dataset_revision=dataset_revision,
        )

    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:24]
    path_hint = next((part for part in parsed.path.split("/") if part), provider)
    return SourceCandidate(
        provider=provider or "unknown",
        company=_humanize(path_hint),
        board_identifier=f"url:{digest}",
        career_page_url=canonical_url.strip(),
        canonical_url=normalized_url,
        support_status="unsupported",
        support_reason="No verified adapter is implemented for this ATS provider",
        dataset_name=dataset_name,
        dataset_revision=dataset_revision,
    )


def parse_dataset_csv(
    content: str,
    *,
    dataset_name: str = "latmay/ats-career-page-urls",
    dataset_revision: str | None = None,
) -> tuple[list[SourceCandidate], list[str]]:
    reader = csv.DictReader(io.StringIO(content))
    required = {"canonical_url", "ats_platform"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError(
            "Dataset CSV must contain canonical_url and ats_platform columns"
        )

    deduplicated: dict[tuple[str, str], SourceCandidate] = {}
    errors: list[str] = []
    for row_number, row in enumerate(reader, start=2):
        url = (row.get("canonical_url") or "").strip()
        platform = (row.get("ats_platform") or "").strip()
        if not url or not platform:
            errors.append(f"row {row_number}: missing canonical_url or ats_platform")
            continue
        try:
            candidate = source_candidate(
                url,
                platform,
                dataset_name=dataset_name,
                dataset_revision=dataset_revision,
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"row {row_number}: {exc}")
            continue
        key = (candidate.provider, candidate.board_identifier)
        existing = deduplicated.get(key)
        if existing is None or candidate.canonical_url < existing.canonical_url:
            deduplicated[key] = candidate
    return list(deduplicated.values()), errors

