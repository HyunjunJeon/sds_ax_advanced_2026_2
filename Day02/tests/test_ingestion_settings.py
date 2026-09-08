from dataclasses import replace

import pytest

from day02.errors import ConfigurationError
from day02.evidence import Metadata
from day02.ingestion.pipeline import load_catalog
from day02.settings import Settings, write_json


def test_invalid_metadata_period():
    with pytest.raises(ValueError):
        Metadata(doc_id="x", title="x", source="x", valid_from="2026-09-08", valid_until="2026-09-07")


def test_duplicate_filenames_rejected_before_ingestion(tmp_path):
    settings = replace(Settings.load(), root=tmp_path)
    records = []
    for folder in ["a", "b"]:
        path = tmp_path / folder / "same.md"
        path.parent.mkdir()
        path.write_text("text")
        records.append({"path": str(path.relative_to(tmp_path)), "metadata":
                        Metadata(doc_id=folder, title=folder, source=folder).model_dump(mode="json")})
    catalog = tmp_path / "catalog.json"
    write_json(catalog, records)
    with pytest.raises(ConfigurationError, match="파일명"):
        load_catalog(settings, catalog)


def test_private_atomic_write(tmp_path):
    path = tmp_path / "private.json"
    write_json(path, {"key": "test"}, private=True)
    if __import__("os").name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_settings_require_both_remote_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENVIKING_URL", "http://localhost:19350")
    monkeypatch.delenv("OPENVIKING_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="모두 필요"):
        Settings.load(tmp_path)
