"""The release zip is what users install, so check its layout."""

import importlib.util
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("package", ROOT / "scripts" / "package.py")
package = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(package)


def test_zip_has_one_top_level_folder_and_no_bytecode(tmp_path):
    (ROOT / "kentucky_stac" / "__pycache__").mkdir(exist_ok=True)
    target = package.build_zip(tmp_path)
    assert target.name == f"kentucky_stac-{package.plugin_version()}.zip"
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    assert {name.split("/")[0] for name in names} == {"kentucky_stac"}
    assert "kentucky_stac/metadata.txt" in names and "kentucky_stac/__init__.py" in names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_zip_entries_are_plain_data_files_with_unix_line_endings(tmp_path):
    """On Windows ZipFile.write marks every entry rwxrwxrwx, which the registry's permissions check flags."""
    target = package.build_zip(tmp_path)
    with zipfile.ZipFile(target) as archive:
        for info in archive.infolist():
            assert info.create_system == 3, info.filename
            assert info.external_attr >> 16 == 0o100644, (info.filename, oct(info.external_attr >> 16))
            assert b"\r\n" not in archive.read(info), info.filename


def test_release_notes_are_the_current_versions_entry():
    entry = package.latest_changelog_entry()
    assert entry.startswith(package.plugin_version() + " - ")
