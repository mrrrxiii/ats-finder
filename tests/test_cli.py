from __future__ import annotations

import json
from datetime import timedelta

from ats_finder.cli import _json_default


def test_json_default_serializes_snapshot_gap_as_seconds() -> None:
    encoded = json.dumps(
        {"snapshot_gap": timedelta(hours=1, seconds=2.5)},
        default=_json_default,
    )

    assert json.loads(encoded) == {"snapshot_gap": 3602.5}
