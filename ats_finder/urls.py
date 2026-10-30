"""Canonical public job-detail URL helpers."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


_APPLICATION_SUFFIXES = frozenset({"apply", "application"})


def job_detail_url(value: str) -> str:
    """Remove a terminal ATS application-path segment from a job URL."""

    stripped = value.strip()
    if not stripped:
        return stripped
    parsed = urlsplit(stripped)
    segments = parsed.path.rstrip("/").split("/")
    if not segments or segments[-1].casefold() not in _APPLICATION_SUFFIXES:
        return stripped
    segments.pop()
    path = "/".join(segments) or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
