from __future__ import annotations

from pathlib import Path

from ats_finder.db import Database, SCHEMA_VERSION


def test_opening_current_schema_succeeds_while_another_connection_is_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent.sqlite3"
    writer = Database(path)
    try:
        with writer.transaction():
            reader = Database(path, timeout_seconds=0.1)
            try:
                assert reader.schema_version == SCHEMA_VERSION
                assert reader.list_sources() == []
            finally:
                reader.close()
    finally:
        writer.close()
