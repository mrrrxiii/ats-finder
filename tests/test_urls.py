from __future__ import annotations

import pytest

from ats_finder.urls import job_detail_url


@pytest.mark.parametrize(
    ("application_url", "expected"),
    [
        (
            "https://jobs.lever.co/acme/123/apply",
            "https://jobs.lever.co/acme/123",
        ),
        (
            "https://jobs.ashbyhq.com/acme/123/application/",
            "https://jobs.ashbyhq.com/acme/123",
        ),
        (
            "https://apply.workable.com/acme/j/ABC123/apply/?source=feed",
            "https://apply.workable.com/acme/j/ABC123",
        ),
        (
            "https://boards.greenhouse.io/acme/jobs/123",
            "https://boards.greenhouse.io/acme/jobs/123",
        ),
    ],
)
def test_job_detail_url_removes_terminal_application_segment(
    application_url: str,
    expected: str,
) -> None:
    assert job_detail_url(application_url) == expected
