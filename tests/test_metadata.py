"""Release hygiene: the plugin's version is written in two places and must agree."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _metadata():
    return (ROOT / "kentucky_stac" / "metadata.txt").read_text(encoding="utf-8")


def test_version_matches_pyproject():
    meta_version = re.search(r"^version=(\S+)$", _metadata(), re.M).group(1)
    project_version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert meta_version == project_version


def test_changelog_leads_with_current_version():
    meta = _metadata()
    version = re.search(r"^version=(\S+)$", meta, re.M).group(1)
    assert re.search(rf"^changelog={re.escape(version)} - ", meta, re.M)


def test_required_registry_fields_present():
    meta = _metadata()
    for key in ("name", "qgisMinimumVersion", "description", "about", "version", "author", "email", "repository", "tracker"):
        assert re.search(rf"^{key}=\S", meta, re.M), f"metadata.txt is missing {key}"
