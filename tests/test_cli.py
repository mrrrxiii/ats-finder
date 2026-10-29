from __future__ import annotations

import json
import sys
from datetime import timedelta

from ats_finder.cli import _json_default, main
from ats_finder.db import Database


def test_json_default_serializes_snapshot_gap_as_seconds() -> None:
    encoded = json.dumps(
        {"snapshot_gap": timedelta(hours=1, seconds=2.5)},
        default=_json_default,
    )

    assert json.loads(encoded) == {"snapshot_gap": 3602.5}


def test_cli_exclude_list_and_include_source(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    database_path = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("ATS_FINDER_DB", str(database_path))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ats-finder",
            "exclude-source",
            "greenhouse",
            "example-company",
            "--reason",
            "not suitable",
        ],
    )
    main()
    excluded = json.loads(capsys.readouterr().out)
    assert excluded["provider"] == "greenhouse"
    assert excluded["board_identifier"] == "example-company"
    assert excluded["reason"] == "not suitable"

    monkeypatch.setattr(sys, "argv", ["ats-finder", "list-exclusions"])
    main()
    listed = json.loads(capsys.readouterr().out)
    assert [row["board_identifier"] for row in listed] == ["example-company"]

    monkeypatch.setattr(
        sys,
        "argv",
        ["ats-finder", "include-source", "greenhouse", "example-company"],
    )
    main()
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"] is True

    with Database(database_path, initialize=False) as database:
        assert database.list_source_exclusions() == []


def test_cli_classify_canada_presence(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    database_path = tmp_path / "cli-classify.sqlite3"
    monkeypatch.setenv("ATS_FINDER_DB", str(database_path))
    monkeypatch.setattr(sys, "argv", ["ats-finder", "classify-canada-presence"])

    main()

    classified = json.loads(capsys.readouterr().out)
    assert classified == {"some": 0, "none": 0, "unknown": 0}
